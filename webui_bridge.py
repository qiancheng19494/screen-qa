"""WebView 版界面：Python 侧 API（供 ui/app.js 通过 pywebview 调用）。

设计要点：
- 界面只负责展示与交互，所有识别/作答/缓存/错题本逻辑都复用 app.run_pipeline；
- 每个耗时动作在独立线程里跑，前端 await 拿结果，不阻塞窗口；
- 返回给前端的一定是 JSON 友好结构（dict/list/str/num）。
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from pathlib import Path

import app as A
import capture
import cloud  # noqa: F401  (保留云端入口，未启用时不影响)
import config as cfg_mod
import extras
import ledger
import history  # noqa: F401
import ocrcheck  # noqa: F401
import plotter
import psocr
import prompts
import questions as qsplit
import reader
import solver as solver_mod
import verify

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"


def _num(x, d=0.0):
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return d


def res_to_dict(r) -> dict:
    """SolveResult → JSON 友好字典（只挑前端要用的字段）。"""
    if r is None:
        return {}
    out = {}
    for f in dataclasses.fields(r) if dataclasses.is_dataclass(r) else []:
        try:
            out[f.name] = getattr(r, f.name)
        except Exception:  # noqa: BLE001
            continue
    out.pop("votes", None)
    out.pop("samples", None)
    try:
        out["reasoning"] = A.pretty_reasoning(out.get("reasoning") or "")
    except Exception:  # noqa: BLE001
        pass
    out["agreement"] = _num(out.get("agreement"))
    out["elapsed"] = _num(out.get("elapsed"))
    out["calls"] = int(_num(out.get("calls")))
    return out


class Bridge:
    """pywebview js_api 对象。方法名即前端 `py('method')` 的名字。"""

    def __init__(self) -> None:
        self.win = None
        self.cfg = cfg_mod.load()
        self._sel_full = None
        self.busy = False
        self.cont = False
        self._cont_thread = None
        self._last_image = None
        self._last_payload = None
        self._lock = threading.Lock()
        self._hk = None

    # ---------- 配置 ----------
    def get_cfg(self) -> dict:
        cfg = dict(self.cfg)
        key = str(cfg.get("api_key") or "")
        cfg["api_key"] = "" if key else ""
        cfg["api_key_masked"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("sk-…" if key else "")
        cfg["version"] = extras.LOCAL_VERSION
        return cfg

    def save_cfg(self, patch: dict) -> dict:
        patch = patch or {}
        for k, v in patch.items():
            if k == "api_key" and not str(v).strip():
                continue
            self.cfg[k] = v
        cfg_mod.save(self.cfg)
        self.apply_window(self.cfg.get("always_on_top"), self.cfg.get("opacity"))
        return {"ok": True, "cfg": self.get_cfg()}

    def apply_window(self, on_top=None, opacity=None) -> dict:
        try:
            if on_top is not None and self.win is not None:
                self.win.on_top = bool(on_top)
            if opacity is not None and self.win is not None:
                self.win.opacity = max(0.6, min(1.0, _num(opacity, 100) / 100.0))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def toggle_pin(self) -> bool:
        on = not bool(self.cfg.get("always_on_top"))
        self.cfg["always_on_top"] = on
        cfg_mod.save(self.cfg)
        self.apply_window(on, None)
        return on

    def win_minimize(self) -> bool:
        try:
            self.win.minimize()
        except Exception:  # noqa: BLE001
            pass
        return True

    def win_close(self) -> bool:
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 作答 ----------
    def _run(self, image_path=None, raw_text=None, bypass_cache=False) -> dict:
        cfg = dict(self.cfg)
        if bypass_cache:
            cfg["cache"] = False
        events: list[tuple[str, str]] = []

        def emit(kind, payload):
            events.append((kind, str(payload)))
            if kind == "status":
                self._js_status(str(payload))

        t0 = time.time()
        try:
            payload = A.run_pipeline(image_path, cfg, emit, raw_text=raw_text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stage": "pipeline"}
        self._last_payload = payload
        if image_path:
            self._last_image = image_path
        if not payload.get("ok"):
            return payload
        items = payload.get("items") or ([payload["result"]] if payload.get("result") else [])
        cost = 0.0
        try:
            tin = sum(int(getattr(r, "tokens_in", 0) or 0) for r in items)
            tout = sum(int(getattr(r, "tokens_out", 0) or 0) for r in items)
            cost = cfg_mod.cost_of(tin, tout, self.cfg)
        except Exception:  # noqa: BLE001
            pass
        out = {
            "ok": True,
            "text": payload.get("text", ""),
            "pretty_q": payload.get("pretty_q", ""),
            "translation": payload.get("translation", ""),
            "backend": payload.get("backend", ""),
            "cached": bool(payload.get("cached")),
            "warnings": payload.get("warnings") or [],
            "elapsed": _num(payload.get("elapsed")) or (time.time() - t0),
            "cost_num": cost,
            "items": [res_to_dict(r) for r in items],
        }
        out["result"] = out["items"][0] if out["items"] else {}
        try:
            self._record(out)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _record(self, out: dict) -> None:
        r = out.get("result") or {}
        cfg_mod.add_record({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "title": (out.get("pretty_q") or out.get("text") or "")[:120],
            "answer": r.get("answer", ""),
            "fill": r.get("fill", ""),
            "confidence": r.get("confidence", ""),
            "model": self.cfg.get("model", ""),
            "elapsed": round(_num(r.get("elapsed")), 2),
            "calls": int(_num(r.get("calls"))),
            "backend": out.get("backend", ""),
            "cached": out.get("cached", False),
            "check": r.get("check", ""),
            "tokens_in": int(_num(r.get("tokens_in"))),
            "tokens_out": int(_num(r.get("tokens_out"))),
            "cost": out.get("cost_num", 0.0),
        })

    def capture(self, mode="region") -> dict:
        """mode: region | full"""
        try:
            if mode == "full":
                img = capture.grab_all()
                label = "整屏"
            else:
                self._js_status("圈选题目…")
                sel = capture.RegionSelector()
                img = sel.select()
                label = "框选"
                if img is None:
                    return {"cancelled": True}
                self.cfg["cont_bbox"] = list(sel.bbox) if getattr(sel, "bbox", None) else None
                cfg_mod.save(self.cfg)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"截图失败：{exc}", "stage": "capture"}
        path = ROOT / "_tmp" / f"web_{int(time.time()*1000)}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        self._js_status(f"{label}完成，识别中…")
        return self._run(str(path))


    # ---------- 页内框选（不依赖 Tk 遮罩，WebView 版专用）----------
    def _data_uri(self, img, quality=72):
        import base64
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    def _sel_screen(self):
        try:
            return capture.screen_size()
        except Exception:  # noqa: BLE001
            return (1920, 1080)

    def _restore_win(self):
        sw, sh = self._sel_screen()
        w, h = 504, min(861, sh - 80)
        x, y = max(sw - w - 26, 0), max(sh - h - 74, 0)
        try:
            self.win.resize(w, h)
            self.win.move(x, y)
        except Exception:  # noqa: BLE001
            pass

    def capture_begin(self) -> dict:
        """整屏截图 + 把窗口铺满屏幕，让页面自己画选择框。"""
        try:
            full = capture.grab_all()
            self._sel_full = full
            sw, sh = self._sel_screen()
            try:
                self.win.resize(int(sw), int(sh))
                self.win.move(0, 0)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12)
            thumb = full.copy()
            thumb.thumbnail((1500, 950))
            return {"ok": True, "img": self._data_uri(thumb),
                    "w": thumb.width, "h": thumb.height,
                    "full_w": full.width, "full_h": full.height}
        except Exception as exc:  # noqa: BLE001
            self._restore_win()
            return {"ok": False, "error": f"截屏失败：{exc}", "stage": "capture"}

    def capture_cancel(self) -> dict:
        self._restore_win()
        self._sel_full = None
        return {"cancelled": True}

    def capture_finish(self, x1, y1, x2, y2, tw=0, th=0) -> dict:
        """把页面坐标换算回原始截图坐标，裁剪后直接作答。"""
        try:
            full = getattr(self, "_sel_full", None)
            if full is None:
                self._restore_win()
                return {"ok": False, "error": "没有截屏缓存，请重新框选", "stage": "capture"}
            fx = full.width / float(tw or full.width)
            fy = full.height / float(th or full.height)
            left, right = sorted((x1 * fx, x2 * fx))
            top, bot = sorted((y1 * fy, y2 * fy))
            box = (int(left), int(top), int(right), int(bot))
            if box[2] - box[0] < 12 or box[3] - box[1] < 12:
                self._restore_win()
                return {"ok": False, "error": "框太小了，再拉大一点", "stage": "capture"}
            img = full.crop(box)
            self.cfg["cont_bbox"] = [box[0], box[1], box[2] - box[0], box[3] - box[1]]
            cfg_mod.save(self.cfg)
            path = ROOT / "_tmp" / f"web_{int(time.time() * 1000)}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path)
            self._sel_full = None
            self._restore_win()
            self._js_status("识别中…")
            return self._run(str(path))
        except Exception as exc:  # noqa: BLE001
            self._restore_win()
            self._sel_full = None
            return {"ok": False, "error": f"裁剪失败：{exc}", "stage": "capture"}

    def solve_text(self, text: str) -> dict:
        text = (text or "").strip()
        if len(text) < 2:
            return {"ok": False, "error": "文字太短了", "stage": "read"}
        return self._run(raw_text=text)

    def paste(self) -> dict:
        try:
            txt = capture.clipboard_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if len(txt.strip()) >= 12:
            return self._run(raw_text=txt.strip())
        try:
            img = capture.clipboard_image()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读剪贴板失败：{exc}", "stage": "paste"}
        if img is None:
            return {"ok": False, "error": "剪贴板里没有图片或足够长的文字", "stage": "paste"}
        path = ROOT / "_tmp" / f"clip_{int(time.time()*1000)}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        return self._run(str(path))

    def open_image(self) -> dict:
        import tkinter as tk
        from tkinter import filedialog
        try:
            root = tk.Tk()
            root.withdraw()
            path = filedialog.askopenfilename(
                title="选择题目截图",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp"), ("全部", "*.*")])
            root.destroy()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"打开文件对话框失败：{exc}", "stage": "open"}
        if not path:
            return {"cancelled": True}
        return self._run(path)

    def redo(self) -> dict:
        if self._last_image:
            return self._run(self._last_image, bypass_cache=True)
        if self._last_payload and self._last_payload.get("text"):
            return self._run(raw_text=self._last_payload["text"], bypass_cache=True)
        return {"ok": False, "error": "还没有可重算的题", "stage": "redo"}

    def copy(self, text: str) -> bool:
        try:
            return bool(capture.clipboard_set_text(str(text)))
        except Exception:  # noqa: BLE001
            return False

    def alt_method(self) -> dict:
        payload = self._last_payload or {}
        text = payload.get("text") or ""
        r = payload.get("result")
        if not text or r is None:
            return {"ok": False, "error": "先做一道题再说"}
        try:
            out = solver_mod.alt_method(
                A.DeepSeekClient(self.cfg.get("api_key", ""), self.cfg.get("base_url", ""),
                                 self.cfg.get("model", "deepseek-chat")),
                text, r.answer, r.reasoning, model=self.cfg.get("model", "deepseek-chat"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "text": out}

    def mark_error(self) -> str:
        payload = self._last_payload or {}
        r = payload.get("result")
        if r is None:
            return "还没有答案可标记"
        key = payload.get("key") or A._cache_key(payload.get("text", ""), self.cfg)
        cfg_mod.add_error(key, payload.get("text", ""), getattr(r, "answer", ""))
        n = len(cfg_mod.load_errors() or {})
        return f"已记入错题本（共 {n} 题），下次遇到同题会多问几路"

    def selfcheck(self) -> str:
        lines = []
        key = str(self.cfg.get("api_key") or "")
        lines.append(("✅ " if key.startswith("sk-") and len(key) > 20 else "❌ ") + "API Key")
        try:
            cli = A.DeepSeekClient(key, self.cfg.get("base_url", ""), self.cfg.get("model", ""))
            bal = cli.balance(timeout=10)
            lines.append(("✅ " if bal.get("ok") else "❌ ") + f"余额 {bal.get('amount')} {bal.get('currency')}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"❌ 余额查询：{exc}")
        lines.append(("✅ " if psocr.available() else "❌ ") + "本地 OCR")
        try:
            st = reader.backend_status()
            ml = st.get("modlens", {})
            lines.append(("✅ " if ml.get("available") else "⚠ ") + "视觉模型 " + str(ml.get("detail", ""))[:60])
        except Exception:  # noqa: BLE001
            lines.append("⚠ 视觉模型状态未知")
        st2 = cfg_mod.daily_stats(self.cfg)
        lines.append(f"今日 {st2['count']} 题 · 约 ¥{st2['cost']:.4f}")
        return "\n".join(lines)

    def balance_text(self) -> str:
        try:
            cli = A.DeepSeekClient(self.cfg.get("api_key", ""), self.cfg.get("base_url", ""),
                                   self.cfg.get("model", "deepseek-chat"))
            info = cli.balance()
            st = cfg_mod.daily_stats(self.cfg)
            return (f"余额 {info.get('amount')} {info.get('currency')}\n"
                    f"今日 {st['count']} 题 · 约 ¥{st['cost']:.4f} · "
                    f"{st['tokens_in'] + st['tokens_out']} tokens")
        except Exception as exc:  # noqa: BLE001
            return f"查余额失败：{exc}"

    def toggle_continuous(self) -> bool:
        self.cont = not self.cont
        if self.cont:
            self._cont_thread = threading.Thread(target=self._cont_loop, daemon=True)
            self._cont_thread.start()
        return self.cont

    def _cont_loop(self) -> None:
        last_sig = ""
        while self.cont:
            time.sleep(max(1.0, _num(self.cfg.get("interval"), 3)))
            if not self.cont:
                break
            try:
                img = (capture.grab_bbox(*self.cfg["cont_bbox"])
                       if self.cfg.get("cont_bbox") else capture.grab_all())
                path = ROOT / "_tmp" / f"cont_{int(time.time())}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                img.save(path)
                txt = reader.read_with_ocr(str(path), quality="standard")
                sig = "".join(ch for ch in (txt.text or "") if ch.isalnum())[:400]
                if sig and sig == last_sig:
                    self._js_status("画面没变化，跳过")
                    continue
                last_sig = sig
                out = self._run(str(path))
                self._js_render(out)
            except Exception as exc:  # noqa: BLE001
                self._js_status(f"连续模式出错：{exc}")


    # ---------- 判卷 / 练习 / 卡片 / 账本 / 历史 ----------
    def judge(self, student: str) -> dict:
        payload = self._last_payload or {}
        question = payload.get("text") or ""
        r = payload.get("result")
        if not question or r is None:
            return {"ok": False, "error": "先做一道题，再判卷"}
        student = (student or "").strip()
        if not student:
            return {"ok": False, "error": "你还没填答案"}
        try:
            cli = A.DeepSeekClient(self.cfg.get("api_key", ""),
                                   self.cfg.get("base_url", ""),
                                   self.cfg.get("model", "deepseek-chat"))
            msgs = [
                {"role": "system", "content": prompts.JUDGE_SYSTEM},
                {"role": "user", "content": f"题目：\n{question[:3000]}\n\n参考答案：{r.answer}\n"
                                            f"（填入应为：{r.fill or r.answer}）\n\n学生答案：{student}"},
            ]
            out = cli.chat(msgs, temperature=0.0, max_tokens=900)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "text": (out or "").strip()}

    def practice(self) -> dict:
        payload = self._last_payload or {}
        question = payload.get("text") or ""
        if not question:
            return {"ok": False, "error": "先做一道题，再生成同类练习"}
        try:
            cli = A.DeepSeekClient(self.cfg.get("api_key", ""),
                                   self.cfg.get("base_url", ""),
                                   self.cfg.get("model", "deepseek-chat"))
            msgs = [
                {"role": "system", "content": prompts.PRACTICE_SYSTEM},
                {"role": "user", "content": question[:3000]},
            ]
            out = cli.chat(msgs, temperature=0.8, max_tokens=1200)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            d = ROOT / "\u7ec3\u4e60"
            d.mkdir(parents=True, exist_ok=True)
            f = d / (time.strftime("%Y%m%d-%H%M%S") + "-\u7ec3\u4e60.md")
            f.write_text(out or "", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "text": (out or "").strip()}

    def export_card(self) -> dict:
        payload = self._last_payload or {}
        r = payload.get("result")
        if r is None:
            return {"ok": False, "error": "先做一道题"}
        shot = getattr(self, "_last_image", "") or ""
        rec = {"title": (payload.get("pretty_q") or payload.get("text") or "")[:200],
               "answer": r.answer, "fill": r.fill, "confidence": r.confidence,
               "elapsed": round(_num(r.elapsed), 1), "model": self.cfg.get("model", "")}
        try:
            d = ROOT / "cards"
            d.mkdir(parents=True, exist_ok=True)
            path = d / (time.strftime("%Y%m%d-%H%M%S") + "-\u5361\u7247.png")
            extras.export_card(rec, shot, str(path))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "path": str(path), "url": Path(path).as_uri()}

    def ledger(self) -> dict:
        try:
            recs = cfg_mod.load_records(500)
            info = ledger.summarize(recs)
            d = ROOT / "_tmp"
            d.mkdir(parents=True, exist_ok=True)
            png = d / "ledger-web.png"
            ledger.draw_chart(recs, days=30, width=560, height=180).save(png)
            by_model = [{"model": k, "n": v[0] if isinstance(v, (list, tuple)) else v}
                        for k, v in (info.get("by_model") or {}).items()]
            return {"ok": True, "summary": {k: info[k] for k in
                                            ("total_n", "total_cost", "today_n", "today_cost")
                                            if k in info},
                    "by_model": by_model, "url": png.as_uri(), "count": len(recs)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def history(self) -> list:
        out = []
        try:
            base = cfg_mod.history_dir()
            files = sorted(base.glob("*.md"), reverse=True)[:40]
            for f in files:
                title = ""
                try:
                    with open(f, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip("# \n")
                            if line:
                                title = line[:80]
                                break
                except Exception:  # noqa: BLE001
                    pass
                out.append({"name": f.name, "title": title,
                            "time": time.strftime("%m-%d %H:%M",
                                                  time.localtime(f.stat().st_mtime))})
        except Exception:  # noqa: BLE001
            pass
        return out

    def export_csv(self) -> dict:
        try:
            p = ROOT / "records.csv"
            n = cfg_mod.export_records_csv(p)
            return {"ok": True, "count": n, "path": str(p)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def recent(self) -> list:
        try:
            base = ROOT / "shots"
            files = sorted(base.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[:8]
            return [{"name": f.name, "url": f.as_uri()} for f in files]
        except Exception:  # noqa: BLE001
            return []

    def plot(self) -> dict:
        payload = self._last_payload or {}
        q = payload.get("text") or (payload.get("pretty_q") or "")
        try:
            exprs = plotter.extract_functions(q)
            if not exprs:
                return {"ok": False, "error": "题目里没找到可画的函数（形如 f(x)=… 或 y=…）"}
            d = ROOT / "_tmp"
            d.mkdir(parents=True, exist_ok=True)
            png = d / "plot-web.png"
            (plotter.plot_multi(exprs) if len(exprs) > 1 else plotter.plot_function(exprs[0])).save(png)
            return {"ok": True, "url": png.as_uri(), "exprs": exprs}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def warmup(self) -> str:
        return self.selfcheck() + "\n\n（WebView 版热身：环境自检已完成；要跑实题请用 Tk 版「考前热身」）"

    # ---------- 前端事件 ----------
    def _js(self, code: str) -> None:
        try:
            if self.win is not None:
                self.win.evaluate_js(code)
        except Exception:  # noqa: BLE001
            pass

    def _js_status(self, text: str) -> None:
        self._js(f"setStatus({json.dumps(text)})")

    def _js_render(self, payload: dict) -> None:
        self._js(f"renderResult({json.dumps(payload, ensure_ascii=False)})")
