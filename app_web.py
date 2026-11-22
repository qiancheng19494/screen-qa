"""屏幕问答 · WebView 版启动器（C 方案：界面交给 HTML/CSS，逻辑仍在 Python）。

用法：
    python app_web.py            # 正常启动
    python app_web.py --dev      # 打开开发者工具
"""
from __future__ import annotations

import ctypes
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:  # noqa: BLE001
    pass
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402  pywebview

import capture  # noqa: E402
import config as cfg_mod  # noqa: E402
import extras  # noqa: E402
import hotkey  # noqa: E402
import prompts  # noqa: E402
import webui_bridge as bridge_mod  # noqa: E402


def enable_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def place_bottom_right(win, w: int, h: int, margin: int = 26) -> None:
    try:
        sw, sh = capture.screen_size()
        work_h = sh - 74  # 任务栏
        win.move(int(sw - w - margin), int(max(0, work_h - h - margin)))
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    enable_dpi()
    dev = "--dev" in sys.argv
    cfg = cfg_mod.load()
    w = int(cfg.get("win_width") or 520)
    h = int(cfg.get("win_height") or 900)

    api = bridge_mod.Bridge()
    url = (ROOT / "ui" / "index.html").as_uri()
    window = webview.create_window(
        "屏幕问答",
        url=url,
        js_api=api,
        width=w,
        height=h,
        min_size=(420, 620),
        frameless=True,
        easy_drag=True,
        background_color="#f2f5fa",
        text_select=True,
    )
    api.win = window

    def hooks():
        time.sleep(0.8)
        api.apply_window(cfg.get("always_on_top"), cfg.get("opacity"))
        place_bottom_right(window, w, h)
        # 全局热键（框选 / 整屏 / 文字 / 重算）
        try:
            hk = hotkey.HotkeyManager()
            hk.add("region", cfg.get("hotkey_region", "ctrl+alt+q"),
                   ["ctrl+shift+q", "ctrl+alt+1"])
            hk.add("full", cfg.get("hotkey_full", "ctrl+alt+w"), ["ctrl+alt+s", "ctrl+shift+w"])
            hk.add("text", cfg.get("hotkey_text", "ctrl+alt+t"), ["ctrl+shift+t"])
            hk.add("redo", cfg.get("hotkey_redo", "ctrl+alt+r"), ["ctrl+shift+r"])
            hk.start()
            api._hk = hk
            print("hotkey:", hk.ok_count, hk.bound, "failed:", hk.failed, flush=True)
        except Exception as exc:  # noqa: BLE001
            print("hotkey failed:", exc)

        def loop():
            while True:
                try:
                    if api._hk is None:
                        break
                    name = api._hk.events.get(timeout=0.4)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    if name == "region":
                        api._js("startRegionSelect()")
                    elif name == "full":
                        api._js_render(api.capture("full"))
                    elif name == "text":
                        api._js_status("按 Ctrl+Alt+T：在「键入题目」里粘贴后重算")
                        api._js("document.getElementById('btnTyped').click()")
                    elif name == "redo":
                        api._js_render(api.redo())
                except Exception as exc:  # noqa: BLE001
                    api._js_status(f"热键处理失败：{exc}")

        threading.Thread(target=loop, daemon=True).start()

        if "--testregion" in sys.argv:
            time.sleep(3.5)
            try:
                r = api.capture_begin()
                print("[tr] begin:", {k: r.get(k) for k in ("ok", "w", "h", "full_w", "full_h")}, flush=True)
                s_ = r["w"] / float(r["full_w"])
                x1, y1 = 80 * s_, 150 * s_
                x2, y2 = 980 * s_, 450 * s_
                out = api.capture_finish(x1, y1, x2, y2, r["w"], r["h"])
                print("[tr] answer:", out.get("ok"), out.get("answer"), out.get("fill"),
                      out.get("confidence"), out.get("check"), flush=True)
            except Exception as exc:  # noqa: BLE001
                print("[tr] failed:", exc, flush=True)

        if "--showmodal" in sys.argv:
            time.sleep(2.0)
            try:
                out = api.solve_text("Solve for x.\n9x + 6 = 60")
                api._js_render(out)
                time.sleep(1.2)
                api._js("document.getElementById('btnLedger').click()")
                print("[showmodal] ledger opened", flush=True)
            except Exception as exc:  # noqa: BLE001
                print("[showmodal] failed:", exc, flush=True)

        if "--probe" in sys.argv:
            time.sleep(2.0)
            try:
                out = api.solve_text("Solve for x.\n9x + 6 = 60")
                api._js_render(out)
                time.sleep(1.5)
                for js, label in [
                    ("document.getElementById('btnLedger').click(); 'ok'", "ledger"),
                    ("document.getElementById('btnHistory').click(); 'ok'", "history"),
                    ("document.getElementById('btnRecent').click(); 'ok'", "recent"),
                    ("document.getElementById('btnPlot').click(); 'ok'", "plot"),
                    ("document.getElementById('btnJudge').click(); 'ok'", "judge"),
                ]:
                    api._js(js)
                    time.sleep(2.2)
                    title = api.win.evaluate_js("document.getElementById('modalTitle').textContent")
                    vis = api.win.evaluate_js("!document.getElementById('modal').hidden")
                    body = api.win.evaluate_js("document.getElementById('modalBody').innerText.slice(0,80)")
                    inp = api.win.evaluate_js("!document.getElementById('modalInput').hidden")
                    print(f"[probe] {label}: visible={vis} title={title!r} input={inp} body={body!r}", flush=True)
                    api._js("closeModal()")
                    time.sleep(0.4)
            except Exception as exc:  # noqa: BLE001
                print("[probe] failed:", exc, flush=True)

        if "--selftest" in sys.argv:
            time.sleep(2.5)
            try:
                out = api.solve_text("Question 3 of 10\nSolve for x.\n9x + 6 = 60")
                api._js_render(out)
            except Exception as exc:  # noqa: BLE001
                api._js_status(f"selftest failed: {exc}")

    threading.Thread(target=hooks, daemon=True).start()
    webview.start(debug=dev)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
