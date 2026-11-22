"""屏幕问答助手 —— 自动截图 + 识别 + DeepSeek 作答（贴右下角的悬浮窗）。

用法：
    python app.py                打开窗口（默认贴在屏幕右下角）
    python app.py --cli          无界面：截全屏 → 识别 → 作答 → 打印
    python app.py --image FILE   直接对一张图片作答
"""
from __future__ import annotations

import argparse
import ctypes
import difflib
import hashlib
import json
import queue
import re
import shutil
import sys
import plotter
import tempfile
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog

def _app_dir() -> "Path":
    """打包成 exe 后，程序目录 = exe 所在目录；源码运行时 = 本文件目录。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_dir()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capture  # noqa: E402
import prompts
import cloud as cloud_mod
import config as cfg_mod  # noqa: E402
import extras
import latexfmt
import ledger
from PIL import ImageTk  # noqa: E402
import questions as qsplit  # noqa: E402
import reader  # noqa: E402
from deepseek_api import DeepSeekClient  # noqa: E402
from solver import SolveResult, Solver  # noqa: E402

APP_TITLE = "屏幕问答 · DeepSeek"
APP_VERSION = extras.LOCAL_VERSION
GITHUB_REPO = extras.GITHUB_REPO

BACKEND_LABEL = {"winocr": "本地OCR", "modlens": "视觉模型"}

READER_LABELS = {
    "自动（先本地OCR）": ["winocr", "modlens"],
    "仅视觉模型": ["modlens"],
    "仅本地 OCR": ["winocr"],
}
MODEL_CHOICES = (("chat 快", "deepseek-chat"),
                 ("R1 强", "deepseek-reasoner"))
MODEL_LABEL2ID = {lbl: mid for lbl, mid in MODEL_CHOICES}
MODEL_ID2LABEL = {mid: lbl for lbl, mid in MODEL_CHOICES}

MODELS = ["deepseek-chat", "deepseek-reasoner"]
SAMPLE_CHOICES = ["1", "2", "3", "5"]

DARK_UI = {
    "bg": "#0d1117",
    "card": "#161b22",
    "card2": "#1b222c",
    "border": "#2a3341",
    "text": "#e6edf3",
    "dim": "#9aa7b4",
    "muted": "#6e7781",
    "accent": "#4d9dff",
    "green": "#3ddc84",
    "accent_bg": "#17263d",
    "green_bg": "#0e2a1d",
    "amber": "#f0b429",
    "amber_bg": "#2a2211",
    "red": "#f67575",
    "red_bg": "#2c1618",
}
LIGHT_UI = {
    "bg": "#f2f5fa",
    "card": "#ffffff",
    "card2": "#f7f9fd",
    "border": "#e3e9f2",
    "text": "#111827",
    "dim": "#4b5563",
    "muted": "#8b95a7",
    "accent": "#3b82f6",
    "green": "#16a34a",
    "accent_bg": "#e6efff",
    "green_bg": "#e3f8ec",
    "amber": "#b45309",
    "amber_bg": "#fdf1dd",
    "red": "#dc2626",
    "red_bg": "#fdeaea",
}
UI = dict(DARK_UI)


def apply_system_accent():
    """Optional: follow the Windows accent colour for the UI accent."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\DWM")
        val, _ = winreg.QueryValueEx(key, "AccentColor")
        winreg.CloseKey(key)
        r, g, b = val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF
        if r + g + b < 60:
            return False
        UI["accent"] = "#%02x%02x%02x" % (r, g, b)
        UI["accent_bg"] = "#%02x%02x%02x" % (r // 5, g // 5, b // 5)
        return True
    except Exception:  # noqa: BLE001
        return False


# 主题色：只换强调色（和它的浅底），布局与文字色不变
ACCENTS = {
    "blue": ("#4d9dff", "#16233a"),
    "sakura": ("#ff7ab6", "#3a1626"),
    "mint": ("#35d6a4", "#0f2f28"),
    "amber": ("#ffc857", "#3a2f16"),
}


def apply_accent(name: str) -> str:
    """把强调色切到预设（auto/blue/sakura/mint/amber）。auto = 不动（跟随系统或默认）。"""
    key = (name or "auto").strip().lower()
    if key == "auto":
        return "auto"
    if key not in ACCENTS:
        key = "blue"
    acc, acc_bg = ACCENTS[key]
    UI["accent"] = acc
    UI["accent_bg"] = acc_bg
    return key


def apply_theme(name: str) -> str:
    """换配色（在建界面之前调用；改完需要重开窗口才彻底生效）。"""
    src = LIGHT_UI if str(name).lower() == "light" else DARK_UI
    UI.clear()
    UI.update(src)
    _is_light = src is LIGHT_UI
    UI["light"] = _is_light
    return "light" if _is_light else "dark"
FONT = "Microsoft YaHei UI"
CONF_CHIP = {
    "高": ("🟢 高置信度", "green"),
    "中": ("🟡 中置信度", "amber"),
    "存疑": ("🔴 存疑 · 请核对", "red"),
}


# ---------------------------------------------------------------- 小工具
def _hex2rgb(h: str):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        return (0, 0, 0)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))



def _round_rect(canv, x1, y1, x2, y2, r, fill, outline="", width=1, tags=""):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canv.create_polygon(pts, smooth=True, splinesteps=24, fill=fill,
                               outline=outline, width=width, tags=tags)


class RoundCard(tk.Frame):
    """圆角卡片容器：外层负责布局，Canvas 画圆角底，内容放进 self.body。"""

    def __init__(self, master, radius=18, fill=None, pad=16, outline=""):
        bg = master["bg"]
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self.radius, self.pad = radius, pad
        self.fill = fill or UI["card"]
        self.outline = outline
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=60)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=self.fill)
        self._win = self.canvas.create_window(pad, pad, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._on_cfg)
        self.body.bind("<Configure>", self._on_body)

    def _on_cfg(self, _ev=None):
        w = max(self.canvas.winfo_width(), 10)
        self.canvas.itemconfigure(self._win, width=max(10, w - self.pad * 2))
        self._draw()

    def _on_body(self, _ev=None):
        h = self.body.winfo_reqheight() + self.pad * 2
        if abs(self.canvas.winfo_reqheight() - h) > 2:
            self.canvas.configure(height=h)
        self._draw()

    def _draw(self):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4 or h < 4:
            return
        c.delete("cardbg")
        _round_rect(c, 1, 1, w - 1, h - 1, self.radius, self.fill,
                    outline=self.outline, width=1, tags="cardbg")
        c.tag_lower("cardbg")


def _gradient(parent, height=2, c1=None, c2=None):
    """一条水平渐变的细线，纯装饰。"""
    c1 = c1 or UI["accent"]
    c2 = c2 or UI["bg"]
    cv = tk.Canvas(parent, height=height, bg=UI["bg"], highlightthickness=0, bd=0)

    def draw(_ev=None):
        w = cv.winfo_width()
        if w <= 1:
            cv.after(80, draw)
            return
        cv.delete("all")
        a, b = _hex2rgb(c1), _hex2rgb(c2)
        for x in range(w):
            t = x / max(1, w - 1)
            col = "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
            cv.create_line(x, 0, x, height, fill=col)

    cv.bind("<Configure>", draw)
    cv.after(90, draw)
    return cv


def place_bottom_right(root: tk.Tk, w: int, h: int, margin: int = 28) -> None:
    """把窗口摆到工作区右下角（避开任务栏）。"""
    x = y = None
    try:
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        r = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            x = r.right - w - margin
            y = r.bottom - h - margin
    except Exception:  # noqa: BLE001
        pass
    if x is None:
        x = root.winfo_screenwidth() - w - margin
        y = root.winfo_screenheight() - h - margin - 48
    root.geometry(f"{w}x{h}+{max(0, int(x))}+{max(0, int(y))}")


def apply_icon(win) -> None:
    """窗口/任务栏图标。优先用 assets/app.ico（含 16~256 多尺寸）。"""
    try:
        ico = ROOT / "assets" / "app.ico"
        if ico.exists():
            win.iconbitmap(default=str(ico))
    except Exception:  # noqa: BLE001
        pass
    try:
        png = ROOT / "assets" / "icon-64.png"
        if png.exists():
            img = tk.PhotoImage(file=str(png))
            win.iconphoto(False, img)
            win._icon_ref = img          # 防止被 GC
    except Exception:  # noqa: BLE001
        pass


def dark_titlebar(root: tk.Tk) -> None:
    """Windows 10/11 深色标题栏。"""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        one = ctypes.c_int(1)
        for attr in (20, 19):          # 20=深色标题栏 19=深色边框
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(one), ctypes.sizeof(one))
        # 33 = DWMWA_WINDOW_CORNER_PREFERENCE，2 = 圆角（Win11 生效，Win10 忽略）
        round_ = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(round_), ctypes.sizeof(round_))
    except Exception:  # noqa: BLE001
        pass


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _workarea() -> "_RECT | None":
    try:
        r = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return r
    except Exception:  # noqa: BLE001
        pass
    return None


def snap_bottom_right(root: tk.Tk, margin: int = 26) -> None:
    """把整个窗口外框（含标题栏）对齐到工作区右下角。"""
    try:
        root.update_idletasks()
        u = ctypes.windll.user32
        hwnd = u.GetParent(root.winfo_id())
        rect, wa = _RECT(), _workarea()
        if not hwnd or wa is None:
            return
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        dx = (wa.right - margin) - rect.right
        dy = (wa.bottom - margin) - rect.bottom
        if abs(dx) <= 1 and abs(dy) <= 1:
            return
        u.SetWindowPos(hwnd, 0, rect.left + dx, rect.top + dy, 0, 0,
                       0x0001 | 0x0004)  # SWP_NOSIZE | SWP_NOZORDER
    except Exception:  # noqa: BLE001
        pass


def _normalize(text: str) -> str:
    keep = []
    for ch in (text or ""):
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            keep.append(ch)
    return "".join(keep)


def _same_screen(a: str, b: str) -> bool:
    """两次识别的画面是否算同一张（容忍 OCR 抖动）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    la, lb = len(a), len(b)
    if min(la, lb) < 8:
        return a == b
    if abs(la - lb) > max(8, int(max(la, lb) * 0.08)):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.97


def _same_problem(a: str, b: str) -> bool:
    """是不是「同一道题」：允许比 _same_screen 更大的差异（光标、计时器、按钮文案微变）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    la, lb = len(a), len(b)
    if min(la, lb) < 12:
        return False
    if abs(la - lb) > max(12, int(max(la, lb) * 0.25)):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.93


# ---------------------------------------------------------------- 核心流程
CACHE_PATH = ROOT / "cache.json"
CACHE_LIMIT = 300


def _cache_key(text: str, cfg: dict) -> str:
    raw = "|".join([_normalize(text), str(cfg.get("model", "")),
                    str(cfg.get("samples", "")), str(cfg.get("verify", "")),
                    str(cfg.get("multi", True))])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _fuzzy_sig(text: str) -> str:
    """模糊签名：题号 + 题面里出现过的数字（排序去重）。

    OCR 抖动多半只影响空格/形近字，数字和题号几乎总是稳定的；
    用这两样当"同一道题"的判据，比整段文本做 sha1 稳得多。
    """
    norm = _normalize(text)
    nums = sorted({f"{n:.6g}" for n in qsplit.numbers_in(text)})
    qno = qsplit.question_number(text) or (0, 0)
    raw = f"{qno[0]}/{qno[1]}|" + ",".join(nums) + "|" + str(len(norm) // 20)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _similar(a: str, b: str) -> float:
    try:
        return difflib.SequenceMatcher(None, a, b).ratio()
    except Exception:  # noqa: BLE001
        return 0.0


def find_fuzzy(hit_cache: dict, text: str, cfg: dict):
    """精确 key 没命中时，再按 题号+数字 找回"同一道题"。"""
    sig = _fuzzy_sig(text)
    norm = _normalize(text)
    best = None
    best_ratio = 0.0
    for key, entry in (hit_cache or {}).items():
        if not isinstance(entry, dict) or not entry.get("items"):
            continue
        if entry.get("sig") != sig:
            continue
        ratio = _similar(norm, entry.get("norm") or "")
        if ratio >= 0.93 and ratio > best_ratio:
            best = (key, entry)
            best_ratio = ratio
    return best


def load_cache() -> dict:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_cache(data: dict):
    try:
        if len(data) > CACHE_LIMIT:
            keep = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0),
                          reverse=True)[:CACHE_LIMIT]
            data = dict(keep)
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def clear_cache() -> int:
    n = len(load_cache())
    try:
        CACHE_PATH.unlink()
    except Exception:  # noqa: BLE001
        pass
    return n


def _res_to_dict(r: SolveResult) -> dict:
    return {
        "question_text": r.question_text, "answer": r.answer, "fill": r.fill,
        "reasoning": r.reasoning, "samples": list(r.samples),
        "sample_details": list(r.sample_details), "confidence": r.confidence,
        "agreement": r.agreement, "verified": r.verified, "elapsed": r.elapsed,
        "calls": r.calls, "error": r.error, "warnings": list(r.warnings),
    }


def _dict_to_res(d: dict) -> SolveResult:
    data = {k: v for k, v in d.items() if k in SolveResult.__annotations__}
    return SolveResult(**data)


def _fast_downgrade(items, cfg):
    """极速模式只问了一次、没交叉复核，标"高"是不诚实的，降到"中"。"""
    if not cfg.get("fast"):
        return
    for it in items:
        if it.confidence.startswith("高"):
            it.confidence = "中"


class _ReadShim:
    """把一段已知文字伪装成"识别结果"，让后面的流程完全复用。"""

    def __init__(self, text, backend="剪贴板文字", note=""):
        self.text = text
        self.backend = backend
        self.warnings = [note or "直接用了剪贴板里的文字，没有截图（更快更准）"]
        self.elapsed = 0.0
        self.error = ""


def run_pipeline(image_path, cfg: dict, emit, raw_text: str | None = None) -> dict:
    t0 = time.time()

    reader_order = cfg.get("reader_order") or ["winocr", "modlens"]
    if cfg.get("vision_first"):
        # 几何图模式：图形/坐标轴本地 OCR 读不出来，必须让视觉模型看整图
        reader_order = ["modlens", "winocr"]
        emit("log", "几何图模式：先用视觉模型读整图…")
    if raw_text is not None:
        rr = _ReadShim(raw_text)
        emit("log", f"纯文字模式：{len(rr.text)} 字（跳过 OCR）")
    else:
        emit("log", f"识别中（{' → '.join(reader_order)}）…")
        rr = reader.read_image(
            image_path,
            order=reader_order,
            modlens_timeout=int(cfg.get("modlens_timeout", 300)),
            log=lambda m: emit("log", m),
            ocr_quality=str(cfg.get("ocr_quality", "high")),
            cross_check=bool(cfg.get("cross_check", True)),
        )

    if not rr.text:
        return {"ok": False, "stage": "read", "error": rr.error,
                "elapsed": time.time() - t0}
    if len(rr.text.strip()) < 12:
        full_key = str(cfg.get("hotkey_full", "ctrl+alt+w")).upper()
        return {"ok": False, "stage": "read", "elapsed": time.time() - t0,
                "error": (f"只识别到 {len(rr.text.strip())} 个字，基本等于没框到题目。"
                          f"\n把题目（含题干和选项）框大一点，或按 {full_key} 整屏。")}
    if cfg.get("clip_fallback", True):
        try:
            import ocrcheck as _oc
            why = _oc.risk(rr.text)
            if why:
                clip = (capture.clipboard_text() or "").strip()
                same_q = False
                if len(clip) >= 20 and re.search(r"[0-9=]", clip):
                    try:
                        import verify as _vf
                        nums_ocr = _vf.extract_numbers(rr.text)
                        nums_clip = _vf.extract_numbers(clip)
                        same_q = any(abs(x - y) < 1e-9 for x in nums_ocr for y in nums_clip)
                    except Exception:  # noqa: BLE001
                        same_q = False
                if same_q:
                    rr = _ReadShim(
                        clip,
                        backend=prompts.MSG_CLIP_BACKEND,
                        note=prompts.MSG_CLIP_NOTE.format(why=why))
                    emit("log", prompts.MSG_CLIP_TAKEN.format(why=why, n=len(clip)))
                else:
                    emit("log", prompts.MSG_CLIP_MISSING.format(why=why))
        except Exception:  # noqa: BLE001
            pass
    emit("log", f"识别完成（{rr.backend}，{len(rr.text)} 字，{rr.elapsed:.1f}s）")
    pretty_q = latexfmt.pretty(rr.text)
    translation = ""
    if cfg.get("translate", True):
        ascii_ratio = sum(ch.isascii() for ch in rr.text) / max(1, len(rr.text))
        if (not cfg.get("translate_only_ascii", True)) or ascii_ratio > 0.6:
            emit("log", prompts.MSG_TRANSLATE_RUN)
            try:
                client = DeepSeekClient(cfg["api_key"], cfg.get("base_url", cfg_mod.DEFAULTS["base_url"]),
                                        cfg.get("model", cfg_mod.DEFAULTS["model"]),
                                        int(cfg.get("timeout", 60)))
                translation = client.chat(
                    [{"role": "system", "content": prompts.TRANSLATE_SYSTEM},
                     {"role": "user", "content": rr.text[:4000]}],
                    temperature=0.0, max_tokens=900, model=cfg.get("model"),
                ).strip()
                translation = latexfmt.pretty(str(translation or '').strip()).replace('$', '').strip()
            except Exception as exc:  # noqa: BLE001
                emit("log", f"翻译失败（不影响作答）：{exc}")

    key = _cache_key(rr.text, cfg)

    # ---- 缓存命中：同一道题做过就直接秒出，不再花钱调模型
    if cfg.get("cache", True):
        all_cache = load_cache()
        hit = all_cache.get(key)
        fuzzy_note = ""
        if not (hit and hit.get("items")) and cfg.get("fuzzy_cache", True):
            found = find_fuzzy(all_cache, rr.text, cfg)
            if found:
                key, hit = found
                fuzzy_note = "（题面略有差异，按题号+数字匹配上的）"
        if hit and hit.get("items"):
            items = [_dict_to_res(d) for d in hit["items"]]
            _fast_downgrade(items, cfg)
            emit("log", f"⚡ 命中缓存{fuzzy_note}"
                        f"（省下 {sum(i.calls for i in items)} 次模型调用）")
            return {
                "ok": True, "text": rr.text, "backend": rr.backend,
                "pretty_q": latexfmt.pretty(rr.text),
                "translation": str(hit.get("translation") or ""),
                "read_elapsed": rr.elapsed, "result": items[0], "items": items,
                "elapsed": time.time() - t0, "warnings": rr.warnings,
                "cached": True, "cached_at": hit.get("ts", 0), "key": key,
                "fuzzy": bool(fuzzy_note),
            }

    # ---- 错题本：同一道题上次被你标记为"答错了" → 这次多问两路
    samples_n = int(cfg.get("samples", 3))
    try:
        err_hit = cfg_mod.load_errors().get(key)
    except Exception:  # noqa: BLE001
        err_hit = None
    if err_hit:
        samples_n = min(5, samples_n + 2)
        emit("log", f"⚠ 这题上次被标记为答错（已标记 {err_hit.get('hits', 1)} 次）"
                    f"→ 本次多问 {samples_n - int(cfg.get('samples', 3))} 路")

    client = DeepSeekClient(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
    )
    # 低置信度时换 deepseek-reasoner 复核：极速模式或已经用 R1 当主模型时跳过
    use_r1 = (bool(cfg.get("reasoner", True)) and not cfg.get("fast")
              and cfg.get("model") != "deepseek-reasoner")
    def _stream_cb(delta: str):
        """流式增量：只往界面发，不改任何状态（失败也不该影响作答）。"""
        try:
            emit("stream", delta)
        except Exception:  # noqa: BLE001
            pass

    solver = Solver(
        client,
        samples=samples_n,
        verify=bool(cfg.get("verify", True)),
        temperature=float(cfg.get("temperature", 0.7)),
        progress=lambda m: emit("log", m),
        escalate=bool(cfg.get("escalate", True)),
        reasoner="deepseek-reasoner" if use_r1 else "",
        dual=bool(cfg.get("dual")) and use_r1 and not cfg.get("fast"),
        adaptive=bool(cfg.get("adaptive", True)) and not cfg.get("fast"),
        stream_cb=_stream_cb if cfg.get("stream", True) else None,
    )

    # ---- 一屏多题：切成一题一题，分别走同一套采样+投票+审核
    chunks = qsplit.split_questions(rr.text) if cfg.get("multi", True) else []
    items: list[SolveResult] = []
    if len(chunks) >= 2:
        emit("log", f"检测到 {len(chunks)} 道题，逐题作答…")
        for i, q in enumerate(chunks, 1):
            emit("log", f"—— 第 {i}/{len(chunks)} 题 ——")
            items.append(solver.solve(q))
    else:
        emit("log", "DeepSeek 思考中…")
        items.append(solver.solve(rr.text))

    _fast_downgrade(items, cfg)

    payload = {
        "ok": True,
        "text": rr.text,
        "backend": rr.backend,
        "read_elapsed": rr.elapsed,
        "result": items[0],
        "items": items,
        "elapsed": time.time() - t0,
        "warnings": rr.warnings,
        "cached": False,
        "key": key,
        "pretty_q": pretty_q,
        "translation": translation,
    }

    # ---- 成功作答才写缓存（失败的不缓存，免得下次直接给出错误结论）
    if (cfg.get("cache", True) and items
            and not items[0].answer.startswith("（调用失败）")):
        try:
            data = load_cache()
            data[key] = {"ts": time.time(), "text": rr.text[:4000],
                         "translation": translation,
                         "items": [_res_to_dict(r) for r in items]}
            save_cache(data)
        except Exception:  # noqa: BLE001
            pass

    return payload


_MARK_RE = re.compile(r"^\s*##\s*(推理|最终答案|填入答案|复核|参考答案)\s*$", re.M)


def pretty_reasoning(text: str) -> str:
    """把模型返回的「## 推理 / ## 最终答案」这类小标题去掉，只留正文。"""
    body = _MARK_RE.sub("", (text or "")).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body or "（无）"



def _item_res(x):
    """多题结果既可能是 SolveResult，也可能是 (题面, SolveResult) 元组。"""
    if isinstance(x, (tuple, list)):
        return x[-1]
    return x

def run_followup(image_path, cfg, emit, prev_question="", prev_answer=""):
    """Box a patch of screen and ask a follow-up question about the current problem.

    Single model call (no voting / no cache); result goes to the detail panel only.
    """
    import time
    from deepseek_api import DeepSeekClient
    t0 = time.time()
    emit("status", "follow-up: reading the boxed part ...")
    try:
        order = cfg.get("reader_order") or ["winocr", "modlens"]
        rr = reader.read_image(image_path, order=order,
                              modlens_timeout=int(cfg.get("modlens_timeout", 45)),
                              log=lambda m: emit("log", m),
                              ocr_quality=str(cfg.get("ocr_quality", "high")))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "read failed: %s" % exc, "elapsed": time.time() - t0}
    if not rr.ok or len((rr.text or "").strip()) < 4:
        return {"ok": False, "error": prompts.FOLLOWUP_TOO_LITTLE, "elapsed": time.time() - t0}
    emit("log", "follow-up text (%s, %d chars): %s" %
         (rr.backend, len(rr.text), (rr.text or "").strip()[:120]))
    user = ("PREVIOUS PROBLEM: %s\n"
            "MY PREVIOUS ANSWER: %s\n"
            "NEWLY BOXED CONTENT:\n%s\n" % ((prev_question or "")[:600],
                                             (prev_answer or "")[:200],
                                             (rr.text or "").strip()[:1500]))
    try:
        client = DeepSeekClient(cfg.get("api_key", ""), cfg.get("base_url", ""),
                                cfg.get("model", "deepseek-chat"))
        out = client.chat([{"role": "system", "content": prompts.FOLLOWUP_SYSTEM},
                           {"role": "user", "content": user}],
                          temperature=0.2, max_tokens=1024)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "model call failed: %s" % exc,
                "elapsed": time.time() - t0}
    return {"ok": True, "text": (out or "").strip(), "read": (rr.text or "").strip(),
            "elapsed": time.time() - t0}


def format_answer(payload: dict) -> str:
    res = payload["result"]
    items = payload.get("items") or [res]
    if res.answer.startswith("（调用失败）"):
        return f"❌ 调用 DeepSeek 失败：{res.error}"
    icon = {"高": "🟢", "中": "🟡", "存疑": "🔴"}.get(res.confidence, "⚪")
    lines = []
    if len(items) > 1:
        lines.append(f"【共 {len(items)} 道题】")
        for i, r in enumerate(items, 1):
            extra = f"   → 填入：{r.fill}" if r.fill else ""
            lines.append(f"{i}. {r.answer}{extra}")
        lines.append("")
    lines.append(f"【最终答案】{res.answer}")
    if res.fill:
        lines.append(f"【填入答案】{res.fill}")
    chk = getattr(res, "check", "") or ""
    if chk == "ok":
        lines.append(f"【代入验算】✅ {res.check_note}")
    elif chk == "fail":
        lines.append(f"【代入验算】⚠ 不通过：{res.check_note}")
    if payload.get("cached"):
        lines.append(f"⚡ 命中缓存：直接秒出（当初算了 {res.elapsed:.1f}s / "
                     f"{res.calls} 次调用，本次只花 {payload['elapsed']:.1f}s）")
    else:
        lines.append(f"{icon} 置信度：{res.confidence}（样本一致度 "
                     f"{res.agreement:.0%}，{res.calls} 次模型调用，"
                     f"{res.elapsed:.1f}s）")
    lines += ["", "—— 详细推理 ——", pretty_reasoning(res.reasoning)]
    if len(res.samples) > 1:
        lines += ["", "—— 各次采样 ——"]
        lines += [f"{i}. {x}" for i, x in enumerate(res.samples, 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------- 圆角按钮
class Pill(tk.Canvas):
    """Canvas 手绘的圆角按钮，带 hover / 按下 / 禁用。"""

    LIGHT_KINDS = {
        "primary": ("#ffffff", "#3b82f6", "#2f6fe0"),
        "ghost": ("#374151", "#eef1f7", "#e3e9f3"),
        "success": ("#ffffff", "#16a34a", "#128a3f"),
        "danger": ("#ffffff", "#dc2626", "#c11f1f"),
        "flat": ("#6b7280", "#f4f7fb", "#e9eef6"),
        "tab": ("#1d4ed8", "#e6efff", "#d8e6ff"),
        "tab_off": ("#6b7280", "#ffffff", "#f1f4f9"),
    }

    @classmethod
    def colors(cls, kind):
        table = cls.LIGHT_KINDS if UI.get("light") else cls.KINDS
        return table.get(kind, table["ghost"])

    KINDS = {
        "primary": ("#4d9dff", "#07121f", "#71b4ff"),
        "ghost": ("#1b222c", "#c9d4e0", "#242d3b"),
        "success": ("#10301e", "#54eda0", "#17452c"),
        "danger": ("#331a1d", "#ff9b9b", "#48262b"),
        "flat": ("#0d1117", "#9aa7b4", "#161b22"),
        "tab": ("#17263d", "#4d9dff", "#1d2f4b"),
        "tab_off": ("#161b22", "#9aa7b4", "#1b222c"),
    }

    def __init__(self, master, text, command=None, kind="ghost", height=34,
                 padx=16, radius=11, font_size=10, bold=False, width=None,
                 bg=None):
        self._c_bg = bg or master["bg"]
        fg, fill, hover = self.colors(kind)
        if kind == "primary" and not UI.get("light"):  # 深色下跟随主题强调色
            _acc = UI.get("accent", "#4d9dff")
            fg, fill, hover = _acc, UI.get("accent_bg", "#16233a"), _acc
        elif kind == "primary":
            _acc = UI.get("accent", "#3b82f6")
            fg, fill, hover = "#ffffff", _acc, _acc
        self._pfont = tkfont.Font(family=FONT, size=font_size,
                                  weight="bold" if bold else "normal")
        w = width or (self._pfont.measure(text) + padx * 2)
        super().__init__(master, width=w, height=height, bg=self._c_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._pw, self._ph = int(w), int(height)
        self._pradius = radius
        self._pfg, self._pfill, self._phover = fg, fill, hover
        self._command = command
        self._enabled = True
        self._text = text
        self._shape = self.create_polygon(
            self._points(), smooth=True, splinesteps=28, fill=fill, outline="")
        self._label = self.create_text(self._pw / 2, self._ph / 2, text=text,
                                       fill=fg, font=self._pfont)
        self.bind("<Enter>", lambda e: self._enabled and self._paint(self._phover))
        self.bind("<Leave>", lambda e: self._enabled and self._paint(self._pfill))
        self.bind("<ButtonPress-1>", lambda e: self._enabled and self._paint(self._phover))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _points(self):
        r = self._pradius
        x1, y1, x2, y2 = 1, 1, self._pw - 1, self._ph - 1
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

    def _paint(self, fill):
        self.itemconfigure(self._shape, fill=fill)

    def _on_release(self, ev):
        self._paint(self._pfill)
        if not self._enabled:
            return
        if 0 <= ev.x <= self._pw and 0 <= ev.y <= self._ph and self._command:
            self._command()

    def set_text(self, text):
        self._text = text
        self.itemconfigure(self._label, text=text)

    def set_kind(self, kind):
        fg, fill, hover = self.colors(kind)
        if kind == "primary":
            _acc = UI.get("accent", "#3b82f6")
            if UI.get("light"):
                fg, fill, hover = "#ffffff", _acc, _acc
            else:
                fg, fill, hover = _acc, UI.get("accent_bg", "#16233a"), _acc
        self._pfg, self._pfill, self._phover = fg, fill, hover
        self._paint(fill)
        self.itemconfigure(self._label, fill=fg)

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.itemconfigure(self._label,
                           fill=self._pfg if enabled else UI["muted"])
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint(self._pfill)


# ---------------------------------------------------------------- 无界面模式

def ensure_bundled_files() -> None:
    """exe 版：把只读资源从临时解包目录搬到 exe 旁边（程序只用 ROOT 相对路径）。"""
    src = getattr(sys, "_MEIPASS", None)
    if not src:
        return
    srcp = Path(src)
    rels = (
        "psocr.ps1",
        "assets/app.ico",
        "assets/icon-64.png",
        "assets/icon-256.png",
        "tools/cli_shims/codex.exe",
        "tools/cli_shims/claude.exe",
        "README.md",
        "docs/详解.md",
    )
    for rel in rels:
        s_path, d_path = srcp / rel, ROOT / rel
        if not s_path.is_file() or d_path.exists():
            continue
        try:
            d_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s_path, d_path)
        except Exception:  # noqa: BLE001
            pass

def cli_main(args) -> int:
    ensure_bundled_files()
    try:
        prompts.set_lang(config.load().get("lang", "zh"))
    except Exception:  # noqa: BLE001
        pass
    # exe（--windowed）里 sys.stdout/stderr 是 None：改写日志文件，CLI 才不会崩
    if sys.stdout is None or sys.stderr is None:
        try:
            _log = open(ROOT / "_cli.log", "a", encoding="utf-8", buffering=1)
        except Exception:  # noqa: BLE001
            import io

            _log = io.StringIO()
        if sys.stdout is None:
            sys.stdout = _log
        if sys.stderr is None:
            sys.stderr = _log
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    cfg = cfg_mod.load()
    if args.image:
        path = args.image
    else:
        capture.enable_dpi_awareness()
        capture.grab_all().save(
            path := str(Path(tempfile.gettempdir()) / "screen_qa_cli.png"))
    payload = run_pipeline(path, cfg, lambda k, p: print(p, file=sys.stderr, flush=True))
    if not payload.get("ok"):
        print(f"失败（{payload.get('stage')}）：{payload.get('error')}", file=sys.stderr)
        return 1
    print(format_answer(payload))
    if args.save:
        cfg_mod.save_history(payload["text"], format_answer(payload))
    return 0


# ---------------------------------------------------------------- 主界面

class MiniBar:
    """迷你答案条：一行答案，半透明、可拖动、点一下复制。"""

    def __init__(self, master, ui, on_copy, on_close):
        self.ui = ui
        self.on_copy = on_copy
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.90)
        except Exception:  # noqa: BLE001
            pass
        self.top.configure(bg=ui["card"])
        self.frame = tk.Frame(self.top, bg=ui["card"], padx=10, pady=6)
        self.frame.pack(fill="both", expand=True)
        self.dot = tk.Label(self.frame, text="●", bg=ui["card"], fg=ui["accent"],
                            font=("Microsoft YaHei UI", 10))
        self.dot.pack(side="left")
        self.lbl = tk.Label(self.frame, text="等待作答…", bg=ui["card"],
                            fg=ui["text"], font=("Microsoft YaHei UI", 13),
                            anchor="w", justify="left")
        self.lbl.pack(side="left", padx=(6, 10))
        self.btn_close = tk.Label(self.frame, text="✕", bg=ui["card"],
                                  fg=ui["muted"], font=("Microsoft YaHei UI", 10))
        self.btn_close.pack(side="right")
        self.btn_close.bind("<Button-1>", lambda _e: on_close())
        for w in (self.frame, self.lbl, self.dot):
            w.bind("<Button-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Double-Button-1>", lambda _e: self.on_copy())
        self.top.geometry("360x40+120+120")

    # 拖动
    def _on_press(self, ev):
        self._drag = (ev.x_root, ev.y_root,
                      self.top.winfo_x(), self.top.winfo_y())

    def _on_drag(self, ev):
        try:
            x0, y0, wx, wy = self._drag
            self.top.geometry(f"+{wx + ev.x_root - x0}+{wy + ev.y_root - y0}")
        except Exception:  # noqa: BLE001
            pass

    def set_text(self, text, key="accent"):
        try:
            self.lbl.configure(text=text[:120] or "等待作答…")
            self.dot.configure(fg=self.ui.get(key, self.ui["accent"]))
            need = tkfont.Font(font=self.lbl.cget("font")).measure(text[:120])
            w = min(760, max(240, need + 90))
            x, y = capture.cursor_pos()
            sw, sh = capture.screen_size()
            x = max(8, min(sw - w - 8, x + 18))
            y = max(8, min(sh - 60, y + 24))
            self.top.geometry(f"{w}x40+{x}+{y}")
        except Exception:  # noqa: BLE001
            pass

    def show(self):
        try:
            self.top.deiconify()
            self.top.lift()
        except Exception:  # noqa: BLE001
            pass

    def destroy(self):
        try:
            self.top.destroy()
        except Exception:  # noqa: BLE001
            pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = cfg_mod.load()
        apply_theme(self.cfg.get("theme", "dark"))
        _acc_pref = str(self.cfg.get("accent") or "auto").lower()
        apply_accent(_acc_pref)
        if _acc_pref == "auto" and self.cfg.get("follow_accent", True):
            apply_system_accent()
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self._anim = 0
        self._anim_job = None
        self._status_base = prompts.L("ready")
        self.hk = None
        self.last_payload = None
        self.prev_hwnd = 0
        self.last_image = None
        self.cont_on = False
        self.cont_region = self._load_region()
        self.stop_evt = threading.Event()
        self.cont_sig = ""
        self.last_problem = ""
        self.cont_qno = None
        self._pulse_job = None
        # 多题连答 / 托盘 / 更新状态
        self.multi_items = []
        self.multi_idx = 0
        self.answers_by_no = {}
        self.recent_shots = []
        self.last_region = None      # 上次框选的屏幕区域（重复区域热键用）
        self._budget_warned = False
        self._bal_warned = False
        self.judge_text = ""
        self._task_t0 = 0.0
        self._slow_warned = False
        self.tray = None
        self.hidden = False
        self.update_info = None
        # v2.6：迷你浮窗 / 纯文字 / 窗口截图 / 函数图像 / 朗读 / 剪贴板监听
        self.mini = None
        self.last_image_path = ""
        self.last_job = None
        self._failed = False
        self.shot_path = ""
        self.plot_img = None
        self._clip_seq = 0

        capture.enable_dpi_awareness()
        self._init_window()
        self._build()
        self._setup_hotkeys()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(60, lambda: dark_titlebar(self.root))
        apply_icon(self.root)
        self.root.after(160, lambda: snap_bottom_right(self.root))
        self.root.after(700, lambda: snap_bottom_right(self.root))
        # 被最小化 / Win+D 之后再恢复时，重新贴回右下角
        for i in range(1, 10):
            self.root.bind(f"<Key-{i}>", lambda _e, n=i - 1: self._hot_copy(n))
        self.root.bind("<Map>", self._on_map)
        self.root.after(1200, self._setup_tray)
        self.root.after(3000, self._watch)
        if self.cfg.get("watch_clip"):
            self.root.after(1000, self._start_clip_watch)
        if self.cfg.get("mini"):
            self.root.after(700, self.toggle_mini)
        if self.cfg.get("check_update", True):
            self.root.after(2600, self.check_update_async)
            self.root.after(3400, lambda: self.check_balance(silent=True))

        try:
            exam = str(self.cfg.get("exam_date") or "").strip()
            if exam:
                import datetime as _dt
                d = _dt.date.fromisoformat(exam)
                days = (d - _dt.date.today()).days
                self.log(f"考试日期 {exam}：还剩 {days} 天" if days >= 0
                         else f"考试日期 {exam} 已过去 {-days} 天")
        except Exception:  # noqa: BLE001
            pass

        if not self.cfg.get("api_key"):
            self.set_status("⚠ 还没填 API Key —— 点右上角 ⚙")
        else:
            self._apply_hotkey_labels()

    # ------------------------------------------------------------ 窗口
    def _init_window(self):
        root = self.root
        root.title(f"{APP_TITLE}")
        root.configure(bg=UI["bg"])
        # 字号缩放：只影响正文和答案（其它控件位置是按 100% 摆的，缩放太大会挤）
        try:
            self.font_scale = max(80, min(160, int(self.cfg.get("font_scale", 100)))) / 100.0
        except Exception:  # noqa: BLE001
            self.font_scale = 1.0
        compact = bool(self.cfg.get("compact"))
        self.dock = str(self.cfg.get("dock") or "corner")
        _w, _h = 474, (759 if compact else 794)
        if self.dock == "free":
            root.resizable(True, True)
            geom = str(self.cfg.get("win_size") or "")
            if not re.match(r"^\d+x\d+[+-]-?\d+[+-]-?\d+$", geom):
                geom = f"{_w}x{_h}+{max(40, (root.winfo_screenwidth() - _w) // 2)}+60"
            try:
                root.geometry(geom)
            except Exception:  # noqa: BLE001
                place_bottom_right(root, _w, _h)
        else:
            root.resizable(False, False)
            place_bottom_right(root, _w, _h)
        pos = str(self.cfg.get("last_pos") or "")
        if self.cfg.get("remember_pos") and re.match(r"^\d+x\d+[+-]\d+[+-]\d+$", pos):
            try:
                root.geometry(pos)
            except Exception:  # noqa: BLE001
                pass
        root.minsize(430, 540)
        self.topmost = bool(self.cfg.get("always_on_top", True))
        root.attributes("-topmost", self.topmost)
        self.apply_opacity(self.cfg.get("opacity", 100), save=False)

    def apply_opacity(self, value, save: bool = True):
        """窗口不透明度 60~100（太透明会看不清答案，所以有下限）。"""
        try:
            pct = max(60, min(100, int(float(value))))
        except Exception:  # noqa: BLE001
            pct = 100
        try:
            self.root.attributes("-alpha", pct / 100.0)
        except Exception:  # noqa: BLE001
            pass
        if save:
            self.cfg["opacity"] = pct
            cfg_mod.save(self.cfg)

    def _on_map(self, _ev=None):
        """窗口从最小化恢复（或 Win+D 后恢复）时，再贴一次右下角。"""
        try:
            if self.root.state() == "normal" and getattr(self, "dock", "corner") == "corner":
                self.root.after(80, lambda: snap_bottom_right(self.root))
        except Exception:  # noqa: BLE001
            pass

    def _load_region(self):
        r = self.cfg.get("cont_bbox")
        try:
            if r and len(r) == 4 and int(r[2]) > 8 and int(r[3]) > 8:
                return tuple(int(v) for v in r)
        except Exception:  # noqa: BLE001
            pass
        return None

    # ------------------------------------------------------------ 界面
    def _build(self):
        bg = UI["bg"]
        wrap = tk.Frame(self.root, bg=bg)
        wrap.pack(fill="both", expand=True, padx=15, pady=(12, 10))

        # ---- 标题行
        try:
            _bn = "banner-light.png" if UI.get("light") else "banner.png"
            self._banner_img = tk.PhotoImage(file=str(ROOT / "assets" / _bn))
            bn = tk.Label(wrap, image=self._banner_img, bg=UI["bg"], bd=0)
            bn.pack(fill="x", pady=(0, 6))
        except Exception:  # noqa: BLE001
            pass
        head = tk.Frame(wrap, bg=bg)
        head.pack(fill="x")
        self.dot = tk.Label(head, text="●", bg=bg, fg=UI["accent"], font=(FONT, 11))
        self.dot.pack(side="left")
        tk.Label(head, text=" 就绪可答题", bg=bg, fg=UI["muted"],
                 font=(FONT, 9)).pack(side="left")

        self.lbl_gear = tk.Label(head, text="⚙", bg=bg, fg=UI["dim"],
                                 font=(FONT, 13), cursor="hand2")
        self.lbl_gear.pack(side="right")
        self.lbl_gear.bind("<Button-1>", lambda e: self.open_settings())
        self.lbl_pin = tk.Label(head, text="📌", bg=bg,
                                fg=UI["accent"] if self.topmost else UI["muted"],
                                font=(FONT, 11), cursor="hand2")
        self.lbl_pin.pack(side="right", padx=(0, 10))
        self.lbl_pin.bind("<Button-1>", lambda e: self.toggle_topmost())
        self.lbl_dock = tk.Label(head, text="\u2b1a" if self.dock == "free" else "\u21f2",
                                 bg=bg, fg=UI["muted"], font=(FONT, 11), cursor="hand2")
        self.lbl_dock.pack(side="right", padx=(0, 8))
        self.lbl_dock.bind("<Button-1>", lambda e: self.toggle_dock())
        if self.dock == "free":
            for _w in (self.banner if hasattr(self, "banner") else head,):
                pass

        # 顶部渐变强调线：让界面有个"标题栏"的层次
        self.grad = _gradient(wrap)
        self.grad.pack(fill="x", pady=(9, 0))

        # ---- 答案卡
        card = RoundCard(wrap, radius=20, fill=UI["card"], pad=18)
        card.pack(fill="x", pady=(4, 14))
        # 左侧 3px 色条：随置信度变色，一眼看出这题靠不靠谱
        self.edge = tk.Frame(card.body, bg=UI["accent"], width=3)
        self.edge.pack(side="left", fill="y")
        inner = tk.Frame(card.body, bg=UI["card"])
        inner.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.ans = tk.Label(inner, text="按 Ctrl+Alt+Q 圈住题目", bg=UI["card"],
                            fg=UI["dim"],
                            font=(FONT, max(12, round(23 * self.font_scale)), "bold"),
                            justify="left", anchor="w", wraplength=380)
        self.ans.pack(fill="x")
        chips = tk.Frame(inner, bg=UI["card"])
        chips.pack(fill="x", pady=(9, 0))
        self.chip = tk.Label(chips, text="待命", bg=UI["card2"], fg=UI["muted"],
                             font=(FONT, 9), padx=9, pady=2)
        self.chip.pack(side="left")
        self.meta = tk.Label(chips, text="", bg=UI["card"], fg=UI["muted"],
                             font=(FONT, 9), anchor="w", justify="left",
                             wraplength=268)
        self.meta.pack(side="left", padx=(9, 0))
        # 一致度细条：比"一致 100%"这行字更直观
        self.agree = tk.Canvas(inner, height=4, bg=UI["card"],
                               highlightthickness=0, bd=0)
        self.agree.pack(fill="x", pady=(9, 0))
        self._agree_ratio = 0.0
        self._agree_color = UI["border"]
        self.card = card
        card.bind("<Configure>", self._on_card_resize)

        # ---- 选项行
        opts = tk.Frame(wrap, bg=bg)
        opts.pack(fill="x", pady=(0, 8))
        tk.Label(opts, text="识 别", bg=bg, fg=UI["muted"], font=(FONT, 8)).pack(side="left")
        self.reader_var = tk.StringVar(value=self._reader_label())
        self.reader_box = tk.OptionMenu(opts, self.reader_var, *READER_LABELS,
                                        command=lambda *_: self._on_reader_change())
        self._style_optionmenu(self.reader_box, width=15)
        self.reader_box.pack(side="left", padx=(6, 10))

        tk.Label(opts, text="模型", bg=bg, fg=UI["muted"], font=(FONT, 9)).pack(side="left")
        self.model_var = tk.StringVar(
            value=MODEL_ID2LABEL.get(self.cfg.get("model", "deepseek-chat"),
                                     MODEL_CHOICES[0][0]))
        self.model_box = tk.OptionMenu(opts, self.model_var,
                                       *[lbl for lbl, _ in MODEL_CHOICES])
        self._style_optionmenu(self.model_box, width=12)
        self.model_box.pack(side="left", padx=(6, 10))

        opts2 = tk.Frame(wrap, bg=bg)
        opts2.pack(fill="x", pady=(0, 8))
        tk.Label(opts2, text="采样", bg=bg, fg=UI["muted"],
                 font=(FONT, 9)).pack(side="left")
        self.samples_var = tk.StringVar(value=str(self.cfg.get("samples", 3)))
        self.samples_box = tk.OptionMenu(opts2, self.samples_var, *SAMPLE_CHOICES)
        self._style_optionmenu(self.samples_box, width=3)
        self.samples_box.pack(side="left", padx=(6, 14))
        self.verify_var = tk.BooleanVar(value=bool(self.cfg.get("verify", True)))
        tk.Checkbutton(opts2, text="交叉审核", variable=self.verify_var, bg=bg,
                       fg=UI["dim"], selectcolor=UI["card2"], activebackground=bg,
                       activeforeground=UI["text"], bd=0, highlightthickness=0,
                       font=(FONT, 9)).pack(side="left")
        self.chk_hint = tk.Label(opts2, text="多问几遍取多数", bg=bg,
                                 fg=UI["muted"], font=(FONT, 8))
        self.chk_hint.pack(side="left", padx=(8, 0))

        # ---- 题面 / 翻译条（默认收起，点「题面」展开；可改字重算）
        self.ex = RoundCard(wrap, radius=16, fill=UI["card"], pad=12)
        ex_head = tk.Frame(self.ex.body, bg=UI["card"])
        ex_head.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(ex_head, text="\u25ae \u9898\u9762\uff08\u53ef\u6539\uff09", bg=UI["card2"],
                 fg=UI["dim"], font=(FONT, 9, "bold")).pack(side="left")
        self.b_recalc = Pill(ex_head, prompts.LBL_RECALC, self.recalc_from_excerpt,
                             kind="primary", width=88, height=26, font_size=9)
        self.b_recalc.pack(side="right", padx=(6, 0))
        self.ex_text = tk.Text(self.ex.body, wrap="word", bg=UI["card"], fg=UI["text"],
                               insertbackground=UI["text"], relief="flat", bd=0,
                               highlightthickness=0, height=3,
                               font=(FONT, max(8, round(int(self.cfg.get("font_size", 11))
                                                        * self.font_scale) - 1)),
                               padx=12, pady=6)
        self.ex_text.pack(fill="x", padx=2)
        self.tr_label = tk.Label(self.ex.body, text="", bg=UI["card"], fg=UI["accent"],
                                 justify="left", anchor="w", wraplength=430,
                                 font=(FONT, max(8, round(int(self.cfg.get("font_size", 11))
                                                          * self.font_scale) - 1)),
                                 padx=12, pady=0)
        self.tr_label.pack(fill="x", padx=2, pady=(0, 8))
        self.ex_visible = False

        # ---- 详情
        det = RoundCard(wrap, radius=18, fill=UI["card2"], pad=10)
        det.pack(fill="both", expand=True)
        self.det = det
        cap = tk.Frame(det.body, bg=UI["card2"])
        cap.pack(side="top", fill="x", padx=14, pady=(9, 0))
        tk.Label(cap, text="\u25ae \u7ed3\u679c\u4e0e\u63a8\u7406", bg=UI["card2"],
                 fg=UI["dim"], font=(FONT, 9, "bold")).pack(side="left")
        self.b_ex = Pill(cap, prompts.LBL_EXCERPT, self.toggle_excerpt,
                         kind="flat", width=44, height=22, font_size=9)
        self.b_ex.pack(side="right")
        self.text = tk.Text(det.body, wrap="word", bg=UI["card2"], fg=UI["text"],
                            insertbackground=UI["text"], relief="flat", bd=0,
                            highlightthickness=0, cursor="arrow",
                            height=5 if self.cfg.get("compact") else 8,
                            font=(FONT, max(8, round(int(self.cfg.get("font_size", 11))
                                                     * self.font_scale))),
                            padx=14, pady=12, spacing1=2, spacing3=3)
        sb = tk.Scrollbar(det.body, command=self.text.yview, width=9, bg=UI["card2"],
                          troughcolor=UI["card2"], activebackground=UI["border"],
                          relief="flat", bd=0, highlightthickness=0)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("answer",
                                font=(FONT, max(10, round(14 * self.font_scale)), "bold"),
                                foreground=UI["green"], spacing1=3, spacing3=6)
        self.text.tag_configure("muted", foreground=UI["dim"])
        self.text.tag_configure("warn", foreground=UI["amber"])
        self.text.tag_configure("err", foreground=UI["red"])
        self.text.tag_configure("h", foreground=UI["accent"],
                                font=(FONT, 10, "bold"), spacing1=8, spacing3=3)
        self.text.bind("<Key>", self._block_keys)

        # ---- 操作（包在一张卡片里，视觉上成组）
        controls = RoundCard(wrap, radius=20, fill=UI["card"], pad=14)
        controls.pack(fill="x", pady=(0, 12), before=det)
        cpad = controls.body
        # ---- 分组标签：作答 / 工具 / 记录（一页只显示一组，窗口更清爽）
        tabs = tk.Frame(cpad, bg=UI["card"])
        tabs.pack(fill="x", pady=(0, 8))
        self.tab_pills = {}
        for _name in ("\u4f5c\u7b54", "\u5de5\u5177", "\u8bb0\u5f55"):
            _p = Pill(tabs, _name, lambda n=_name: self._show_page(n),
                      kind="tab_off", width=54, height=24, font_size=9)
            _p.pack(side="left", padx=(0, 6))
            self.tab_pills[_name] = _p
        self.pages = {_n: tk.Frame(cpad, bg=UI["card"])
                      for _n in ("\u4f5c\u7b54", "\u5de5\u5177", "\u8bb0\u5f55")}
        bar = tk.Frame(self.pages["\u4f5c\u7b54"], bg=UI["card"])
        bar.pack(fill="x")
        self.b_region = Pill(bar, prompts.L("capture"), self.shot_region, kind="primary",
                             bold=True, width=96)
        self.b_region.pack(side="left")
        self.b_full = Pill(bar, prompts.L("full"), self.shot_full, kind="ghost", width=70)
        self.b_full.pack(side="left", padx=(7, 0))
        self.b_paste = Pill(bar, prompts.L("paste"), self.paste_image, kind="ghost", width=58)
        self.b_paste.pack(side="left", padx=(7, 0))
        self.b_copy = Pill(bar, prompts.L("copy"), self.copy_answer, kind="ghost", width=76)
        self.b_copy.pack(side="right")
        self.b_copy.set_enabled(False)
        pass

        # 第三行：函数图像 / 朗读 / 迷你浮窗
        bar3 = tk.Frame(self.pages["\u5de5\u5177"], bg=UI["card"])
        bar3.pack(fill="x")
        self.b_plot = Pill(bar3, prompts.L("plot"), self.show_plot, kind="ghost", width=44)
        self.b_plot.pack(side="left")
        self.b_plot.set_enabled(False)
        self.b_speak = Pill(bar3, prompts.L("speak"), self.speak_answer, kind="ghost", width=44)
        self.b_speak.pack(side="left", padx=(7, 0))
        self.b_speak.set_enabled(False)
        self.b_mini = Pill(bar3, prompts.L("mini"), self.toggle_mini, kind="ghost", width=44)
        self.b_mini.pack(side="left", padx=(7, 0))
        self.b_follow = Pill(bar3, prompts.LBL_FOLLOWUP, self.ask_followup,
                             kind="ghost", width=48)
        self.b_follow.pack(side="left", padx=(7, 0))
        self.b_follow.set_enabled(False)
        self.b_recent = Pill(bar3, prompts.LBL_RECENT, self.recent_menu,
                             kind="ghost", width=54)
        self.b_recent.pack(side="left", padx=(7, 0))
        self.b_typed = Pill(bar3, prompts.TYPED_LABEL, self.ask_typed,
                            kind="ghost", width=58)
        self.b_typed.pack(side="left", padx=(7, 0))
        self.b_open = Pill(bar3, prompts.OPEN_LABEL, self.open_image,
                           kind="ghost", width=56)
        self.b_open.pack(side="left", padx=(7, 0))

        # 「步骤 / 错了」放到第四行左侧，避免第三行（7 个按钮）溢出
        _bar4_placeholder = None

        # 第四行：判卷 / 练一练 / 导出卡片 / 重复上次区域
        bar4 = tk.Frame(self.pages["\u5de5\u5177"], bg=UI["card"])
        bar4.pack(fill="x", pady=(8, 0))
        self.b_steps = Pill(bar4, prompts.L("steps"), self.copy_steps,
                            kind="ghost", width=52)
        self.b_steps.pack(side="left", padx=(0, 7))
        self.b_steps.set_enabled(False)
        self.b_bad = Pill(bar4, prompts.L("bad"), self.mark_error,
                          kind="ghost", width=52)
        self.b_bad.pack(side="left", padx=(0, 7))
        self.b_bad.set_enabled(False)
        self.b_judge = Pill(bar4, prompts.JUDGE_LABEL, self.judge_answer,
                            kind="ghost", width=64)
        self.b_judge.pack(side="left")
        self.b_judge.set_enabled(False)
        self.b_practice = Pill(bar4, prompts.PRACTICE_LABEL, self.practice,
                               kind="ghost", width=64)
        self.b_practice.pack(side="left", padx=(7, 0))
        self.b_practice.set_enabled(False)
        self.b_card = Pill(bar4, prompts.CARD_LABEL, self.export_card,
                           kind="ghost", width=58)
        self.b_card.pack(side="left", padx=(7, 0))
        self.b_card.set_enabled(False)
        bar5 = tk.Frame(self.pages["\u5de5\u5177"], bg=UI["card"])
        bar5.pack(fill="x", pady=(8, 0))
        self.b_latex = Pill(bar5, prompts.LATEX_LABEL, self.copy_latex,
                            kind="ghost", width=74)
        self.b_latex.pack(side="left")
        self.b_latex.set_enabled(False)
        self.b_alt = Pill(bar5, prompts.ALT_LABEL, self.alt_method,
                          kind="ghost", width=62)
        self.b_alt.pack(side="left", padx=(7, 0))
        self.b_alt.set_enabled(False)
        self.b_repeat = Pill(bar5, prompts.REPEAT_LABEL, self.repeat_region,
                             kind="ghost", width=76)
        self.b_repeat.pack(side="left", padx=(7, 0))

        # 一屏多题时，这里会冒出「复制第 N 题」按钮；空的时候不占高度
        self.multi_bar = tk.Frame(wrap, bg=bg)
        self.multi_bar.pack(fill="x", before=controls)

        # 最近几条答案：考试时往回翻、重新粘，不用回到原题
        self.hist_bar = tk.Frame(wrap, bg=bg)
        self.hist_bar.pack(fill="x", before=controls)

        bar2 = tk.Frame(self.pages["\u4f5c\u7b54"], bg=UI["card"])
        bar2.pack(fill="x", pady=(8, 0))
        self.b_cont = Pill(bar2, prompts.L("cont"), self.toggle_continuous,
                           kind="success", bold=True, width=108)
        self.b_cont.pack(side="left")
        self.region_lbl = tk.Label(bar2, text=self._region_text(), bg=UI["card"],
                                   fg=UI["muted"], font=(FONT, 9), cursor="hand2")
        self.region_lbl.pack(side="left", padx=(9, 0))
        self.region_lbl.bind("<Button-1>", lambda e: self.region_menu())
        self.interval_lbl = tk.Label(bar2, text=f"每 {self.cfg.get('interval', 3)} 秒",
                                     bg=UI["card"], fg=UI["muted"], font=(FONT, 9),
                                     cursor="hand2")
        self.interval_lbl.pack(side="left", padx=(10, 0))
        self.interval_lbl.bind("<Button-1>", lambda e: self.open_settings())
        # 右侧：历史面板 / 重算（把这两个从主按钮行挪出来，主行才不会挤爆）
        self.b_redo = Pill(bar2, prompts.L("redo"), self.redo, kind="ghost", width=52)
        self.b_redo.pack(side="right")
        self.b_redo.set_enabled(False)
        self.b_hist = Pill(bar2, prompts.L("history"), self.open_history, kind="ghost", width=50)
        self.b_hist.pack(side="right", padx=(0, 7))
        self.b_win = Pill(bar2, prompts.L("win"), self.capture_window_menu, kind="ghost", width=52)
        self.b_win.pack(side="right", padx=(0, 7))

        _rec = self.pages["\u8bb0\u5f55"]
        _r1 = tk.Frame(_rec, bg=UI["card"])
        _r1.pack(fill="x")
        for _txt, _cmd in ((prompts.LBL_LEDGER, self.open_ledger),
                           (prompts.L("history"), self.open_history),
                           (prompts.QUEUE_LABEL, self.retry_queue),
                           ("\u81ea\u68c0", self.selfcheck),
                           ("\u89c6\u89c9\u81ea\u68c0", self._vision_selftest)):
            _p = Pill(_r1, _txt, _cmd, kind="ghost", width=64 if len(_txt) > 3 else 52)
            _p.pack(side="left", padx=(0, 7))
        tk.Label(_rec, text="\u8bb0\u5f55\u5728 records.jsonl / errors.json / history\uff0c\u4e0d\u4f1a\u4e0a\u4f20",
                 bg=UI["card"], fg=UI["muted"], font=(FONT, 8)).pack(anchor="w", pady=(8, 0))
        self._show_page("\u4f5c\u7b54")

        # ---- 状态栏
        st = tk.Frame(self.root, bg=UI["card2"])
        st.pack(fill="x", side="bottom", before=wrap)
        # 右侧先占位（贴右不被挤），左侧状态过长时自行截断
        self.st_right = tk.Label(st, text="", bg=UI["card2"], fg=UI["muted"],
                                 font=(FONT, 8), anchor="e")
        self.st_right.pack(side="right", padx=12, pady=4)
        self.status = tk.Label(st, text="就绪", bg=UI["card2"], fg=UI["muted"],
                               font=(FONT, 9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=12, pady=4)

    def _show_page(self, name):
        """切到某一组按钮（作答 / 工具 / 记录）。"""
        for n_, f_ in getattr(self, "pages", {}).items():
            if n_ == name:
                f_.pack(fill="x", pady=(6, 0))
                for _i, _pad in enumerate((3, 1, 0)):
                    self.root.after(45 * (_i + 1),
                                    lambda p=_pad, fr=f_: fr.pack_configure(pady=(p, 0)))
            else:
                f_.pack_forget()
        for n_, pill_ in getattr(self, "tab_pills", {}).items():
            try:
                pill_.set_kind("tab" if n_ == name else "tab_off")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _style_optionmenu(menu_btn, width=12):
        menu_btn.configure(bg=UI["card"], fg=UI["dim"], activebackground=UI["card"],
                           activeforeground=UI["text"], bd=0, highlightthickness=0,
                           relief="flat", width=width, anchor="w",
                           font=(FONT, 9), cursor="hand2", padx=6)
        try:
            menu_btn["menu"].configure(bg=UI["card"], fg=UI["text"],
                                       activebackground=UI["accent"],
                                       activeforeground="#0a0c10",
                                       bd=0, font=(FONT, 9))
        except Exception:  # noqa: BLE001
            pass

    def _on_card_resize(self, ev):
        self.ans.configure(wraplength=max(200, ev.width - 34))
        try:
            self._set_agree(self._agree_ratio, self._agree_color)
        except Exception:  # noqa: BLE001
            pass

    def _set_agree(self, ratio, color):
        """画一致度细条：底色全宽、彩色部分按比例。"""
        self._agree_ratio = float(ratio or 0.0)
        self._agree_color = color or UI["border"]
        if not hasattr(self, "agree"):
            return
        w = self.agree.winfo_width()
        if w <= 1:
            w = 360
        self.agree.delete("all")
        self.agree.create_rectangle(0, 1, w, 3, fill=UI["border"], outline="")
        span = max(0, min(1.0, self._agree_ratio)) * w
        if span >= 1:
            self.agree.create_rectangle(0, 1, span, 3, fill=self._agree_color, outline="")

    def _refresh_status_meta(self):
        bits = []
        _has_bal = bool(getattr(self, "_balance_text", ""))
        if not _has_bal:
            try:
                bits.append("采样 " + str(self.samples_var.get()))
            except Exception:  # noqa: BLE001
                pass
        n = getattr(self, "_today_count", 0)
        if n and not getattr(self, "_balance_text", ""):
            bits.append(f"今日 {n} 题")
        _bal = getattr(self, "_balance_text", "")
        if _bal:
            bits.append(_bal)
        if not _has_bal:
            n0 = getattr(self, "_today_count", 0)
            if n0:
                pass
        bits.append(prompts.VERSION_LABEL)
        if hasattr(self, "st_right"):
            self.st_right.configure(text=" · ".join(bits))

    def _block_keys(self, ev):
        if ev.state & 0x4 and ev.keysym.lower() in ("c", "a", "insert"):
            return None
        if ev.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next",
                         "Home", "End", "Shift_L", "Shift_R", "Control_L",
                         "Control_R", "Tab"):
            return None
        return "break"

    def _reader_label(self):
        order = self.cfg.get("reader_order") or ["winocr", "modlens"]
        for label, val in READER_LABELS.items():
            if val == order:
                return label
        return list(READER_LABELS)[0]

    def _on_reader_change(self):
        self.cfg["reader_order"] = READER_LABELS.get(
            self.reader_var.get(), ["modlens", "winocr"])
        cfg_mod.save(self.cfg)

    # ------------------------------------------------------------ 状态
    def log(self, msg: str):
        self.set_status(str(msg).strip()[:80])
        try:
            with open(ROOT / "_app.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:  # noqa: BLE001
            pass

    def note(self, msg: str):
        """只写 _app.log，不打断状态栏（后台噪声用）。"""
        try:
            with open(ROOT / "_app.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:  # noqa: BLE001
            pass

    def set_status(self, msg: str):
        try:
            self._refresh_status_meta()
        except Exception:  # noqa: BLE001
            pass
        self._status_base = msg
        self.status.configure(text=msg + (self._dots() if self.busy else ""))

    def _dots(self) -> str:
        return ("· ", "·· ", "··· ")[self._anim % 3]

    def _start_anim(self):
        if self._anim_job:
            return
        def tick():
            self._anim += 1
            if self.busy:
                self.status.configure(
                    text=(self._status_base or "作答中") + " " + self._dots().strip())
                self.dot.configure(fg=UI["accent"] if self._anim % 2 else UI["accent_bg"])
                self._anim_job = self.root.after(380, tick)
            else:
                self._anim_job = None
        self._anim_job = self.root.after(380, tick)

    def _stop_anim(self):
        if self._anim_job:
            try:
                self.root.after_cancel(self._anim_job)
            except Exception:  # noqa: BLE001
                pass
            self._anim_job = None
        self.dot.configure(fg=UI["accent"])
        self.set_status(self._status_base or "就绪")

    def _copy_flash(self, label: str = ""):
        """复制成功时给按钮一个明确回执（考试时很需要这种确认）。"""
        try:
            self.b_copy.set_text("已复制 ✓" if not label else label)
            self.b_copy.set_kind("success")
            self.root.after(1100, lambda: (self.b_copy.set_text("复制答案"),
                                           self.b_copy.set_kind("ghost")))
        except Exception:  # noqa: BLE001
            pass

    def _flash(self, color: str):
        try:
            self.card.outline = color
            self.card._draw()
            self.root.after(420, lambda: self.card.configure(
                highlightbackground=UI["border"]))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 提示音
    def _beep(self, kind: str = "done"):
        """听觉反馈：new=发现新题 / done=答案好了 / warn=出错（设置里可关）。"""
        if not self.cfg.get("sound", True):
            return

        def play():
            try:
                import winsound
                if kind == "new":
                    winsound.Beep(880, 70)
                    winsound.Beep(1180, 90)
                elif kind == "warn":
                    winsound.MessageBeep(0x00000030)  # MB_ICONWARNING
                else:
                    winsound.MessageBeep(0x00000040)  # MB_ICONASTERISK
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=play, daemon=True).start()

    def toggle_dock(self):
        """贴右下角 ↔ 自由窗口（自由模式下可缩放、可拖动、记住位置尺寸）。"""
        self.dock = "free" if getattr(self, "dock", "corner") == "corner" else "corner"
        self.cfg["dock"] = self.dock
        cfg_mod.save(self.cfg)
        if self.dock == "free":
            self.root.resizable(True, True)
            try:
                self.lbl_dock.configure(text="\u2b1a")
            except Exception:  # noqa: BLE001
                pass
            self.log("\u5df2\u5207\u5230\u81ea\u7531\u7a97\u53e3\uff1a\u53ef\u62d6\u52a8\u3001\u53ef\u62c9\u4f38\uff0c\u4f4d\u7f6e\u4f1a\u8bb0\u4f4f")
        else:
            self.root.resizable(False, False)
            try:
                self.lbl_dock.configure(text="\u21f2")
            except Exception:  # noqa: BLE001
                pass
            place_bottom_right(self.root, 474, 759 if self.cfg.get("compact") else 794)
            try:
                snap_bottom_right(self.root)
            except Exception:  # noqa: BLE001
                pass
            self.log("\u5df2\u8d34\u56de\u53f3\u4e0b\u89d2")

    def toggle_topmost(self):
        self.topmost = not self.topmost
        try:
            self.root.attributes("-topmost", self.topmost)
        except Exception:  # noqa: BLE001
            pass
        self.lbl_pin.configure(fg=UI["accent"] if self.topmost else UI["muted"])
        self.cfg["always_on_top"] = self.topmost
        cfg_mod.save(self.cfg)
        self.set_status("窗口已置顶" if self.topmost else "窗口已取消置顶")

    # ------------------------------------------------------------ 截图动作
    def shot_region(self):
        if self.busy:
            return
        self.prev_hwnd = capture.foreground_window()
        try:
            sel = capture.RegionSelector(self.root)
            img = sel.select()
            if img is None:
                self.set_status("已取消")
                return
            self._start(img)
        except Exception as exc:  # noqa: BLE001
            self.log(f"截图失败：{exc}")

    def shot_full(self):
        if self.busy:
            return
        self.prev_hwnd = capture.foreground_window()
        try:
            self._start(self._grab_screen())
        except Exception as exc:  # noqa: BLE001
            self.log(f"截图失败：{exc}")

    def _grab_screen(self):
        """整屏截图，但先把自己藏起来，免得截到自己。"""
        self.last_region = None
        win = self.root
        was = win.state()
        try:
            win.withdraw()
            win.update_idletasks()
            time.sleep(0.12)
            img = capture.grab_all()
        finally:
            try:
                win.deiconify()
                if was == "zoomed":
                    win.state("zoomed")
            except Exception:  # noqa: BLE001
                pass
        return img

    def paste_image(self):
        if self.busy:
            return
        self.prev_hwnd = capture.foreground_window()
        try:
            from PIL import Image, ImageGrab

            img = ImageGrab.grabclipboard()
            if img is None:
                self.log("剪贴板里没有图片")
                return
            if isinstance(img, list):
                img = Image.open(img[0])
            self._start(img)
        except Exception as exc:  # noqa: BLE001
            self.log(f"读取剪贴板失败：{exc}")

    def _start(self, img):
        if self.busy:
            return
        self._task_t0 = time.time()
        self._slow_warned = False
        if not self.cfg.get("api_key"):
            self.log("⚠ 缺少 DeepSeek API Key")
            self.open_settings()
            return
        tmp = Path(tempfile.gettempdir()) / f"screen_qa_{int(time.time()*1000)}.png"
        img.save(tmp)
        self.last_image = tmp
        self._run_path(tmp, bypass=False)
        return

    def _run_path(self, path, bypass=False):
        self._reset_stream()
        if self.busy:
            return
        self.last_image_path = str(path)
        self.last_job = ("image", str(path))
        try:
            if self.cfg.get("autocrop", True):
                from PIL import Image as _Img
                _src = _Img.open(str(path))
                _cut = capture.autocrop(_src)
                if _cut.size != _src.size:
                    _tmp = self._shot_path()
                    _cut.save(_tmp)
                    path = str(_tmp)
        except Exception:  # noqa: BLE001
            pass
        self._failed = False
        self.busy = True
        self._set_buttons(False)
        self.set_status("处理中…")
        self._set_pulse(True)

        cfg = self._current_cfg()
        if bypass:
            cfg["cache"] = False

        def emit(kind, payload):
            self.q.put((kind, payload))

        def work():
            try:
                payload = run_pipeline(str(path), cfg, emit)
            except Exception:  # noqa: BLE001
                payload = {"ok": False, "stage": "crash",
                           "error": traceback.format_exc()[-900:]}
            self.q.put(("finish", payload))

        threading.Thread(target=work, daemon=True).start()

    def _current_cfg(self):
        cfg = dict(self.cfg)
        cfg["reader_order"] = READER_LABELS.get(self.reader_var.get(),
                                                ["winocr", "modlens"])
        if cfg.get("vision_first"):
            cfg["reader_order"] = ["modlens", "winocr"]
        cfg["model"] = MODEL_LABEL2ID.get(self.model_var.get(), "deepseek-chat")
        cfg["samples"] = int(self.samples_var.get())
        cfg["verify"] = bool(self.verify_var.get())
        if cfg.get("fast"):
            cfg["samples"] = 1        # 极速：只问一次
            cfg["verify"] = False     # 不做交叉审核
            cfg["escalate"] = False   # 不追加采样
        elif getattr(self, "_slow_hint", False):
            # 上一次超时了：这一题先用单次采样 + 自适应验算，别再等三路
            cfg["samples"] = 1
            cfg["escalate"] = False
            self._slow_hint = False
            self.log("上次作答超时 → 这次先用单次采样（自适应验算兜底）")
        return cfg

    def _set_buttons(self, enabled: bool):
        for b in (self.b_region, self.b_full, self.b_paste, self.b_repeat):
            b.set_enabled(enabled)
        if not enabled:
            for b in (self.b_copy, self.b_steps, self.b_bad, self.b_redo,
                      self.b_plot, self.b_speak, self.b_judge,
                      self.b_practice, self.b_card, self.b_latex,
                      self.b_alt):
                try:
                    b.set_enabled(False)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------ 连续模式
    def _region_text(self):
        r = self.cont_region
        if not r:
            return "区域：整个屏幕"
        return f"区域：{r[2]}×{r[3]}"

    def pick_region(self, quiet=False):
        if self.busy:
            return None
        try:
            sel = capture.RegionSelector(self.root)
            img = sel.select()
        except Exception as exc:  # noqa: BLE001
            self.log(f"框选失败：{exc}")
            return None
        if img is None:
            if not quiet:
                self.set_status("已取消")
            return None
        if sel.bbox:
            self.last_region = sel.bbox
            self.cont_region = sel.bbox
            self.cfg["cont_bbox"] = list(sel.bbox)
            cfg_mod.save(self.cfg)
        self.region_lbl.configure(text=self._region_text())
        self._warn_overlap()
        return img

    def _warn_overlap(self):
        r = self.cont_region
        if not r:
            return
        try:
            self.root.update_idletasks()
            wx, wy = self.root.winfo_rootx(), self.root.winfo_rooty()
            ww, wh = self.root.winfo_width(), self.root.winfo_height()
        except Exception:  # noqa: BLE001
            return
        if (wx < r[0] + r[2] and r[0] < wx + ww
                and wy < r[1] + r[3] and r[1] < wy + wh):
            self.set_status("⚠ 采集区域与窗口重叠，建议重新圈选")

    def toggle_continuous(self):
        if self.cont_on:
            self.stop_continuous()
            return
        if not self.cfg.get("api_key"):
            self.log("⚠ 先填 API Key")
            self.open_settings()
            return
        if self.cont_region is None:
            ans = messagebox.askyesnocancel(
                "连续截图",
                "先圈出要连续识别的区域？（推荐）\n\n"
                "是 = 现在圈选\n否 = 整个屏幕\n取消 = 不开启")
            if ans is None:
                return
            if ans:
                img = self.pick_region(quiet=True)
                if img is None:
                    self.set_status("没选到区域，已取消")
                    return
        self.start_continuous()

    def start_continuous(self):
        if self.cont_on:
            return
        self.cont_on = True
        self.stop_evt.clear()
        self.cont_sig = ""
        self.b_cont.set_text("■  停止")
        self.b_cont.set_kind("danger")
        self._set_pulse(True)
        self.set_status("连续模式已开启")
        self._warn_overlap()
        threading.Thread(target=self._cont_loop, daemon=True).start()

    def stop_continuous(self):
        self.cont_on = False
        self.stop_evt.set()
        self.b_cont.set_text("▶  连续截图")
        self.b_cont.set_kind("success")
        self._set_pulse(False)
        self.set_status("连续模式已停止")

    def _shot_path(self) -> Path:
        """连续模式的工作图路径：放在程序目录下（系统临时目录有时会被拒绝写入）。"""
        d = ROOT / "_tmp"
        try:
            d.mkdir(exist_ok=True)
            for old in d.glob("cont_*.png"):
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        App._tmp_seq = getattr(App, "_tmp_seq", 0) + 1
        return d / f"cont_{App._tmp_seq:05d}.png"

    def _cont_log(self, msg: str, every: float = 60.0):
        """跳过类消息最多每 60 秒记一次日志，避免刷爆 _app.log。"""
        now = time.time()
        if now - getattr(self, "_cont_log_t", 0.0) > every:
            self._cont_log_t = now
            self.q.put(("log", msg))

    def _cont_loop(self):
        interval = max(1.0, float(self.cfg.get("interval", 3)))
        while not self.stop_evt.is_set():
            try:
                self._cont_once(interval)
            except Exception:  # noqa: BLE001
                self.q.put(("log", "连续模式出错（已跳过本轮）："
                            + traceback.format_exc()[-400:]))
                self.q.put(("busy", False))
                self.stop_evt.wait(interval)

    def _cont_once(self, interval: float):
        t0 = time.time()
        if self.busy:
            self.stop_evt.wait(0.4)
            return
        try:
            img = (capture.grab_bbox(*self.cont_region) if self.cont_region
                   else capture.grab_all())
        except Exception as exc:  # noqa: BLE001
            self.q.put(("log", f"截图失败：{exc}"))
            self.stop_evt.wait(interval)
            return

        tmp = self._shot_path()
        try:
            img.save(tmp)
        except Exception as exc:  # noqa: BLE001
            self.q.put(("log", f"保存截图失败：{exc}"))
            self.stop_evt.wait(interval)
            return

        self.q.put(("status", "对比画面…"))
        try:
            quick = reader.read_with_ocr(str(tmp))
            sig = _normalize(quick.text)
        except Exception:  # noqa: BLE001
            sig = ""
        if len(sig) < 12:
            self.q.put(("status", "画面文字太少，跳过"))
            self._cont_log(f"画面文字太少（{len(sig)} 字），跳过")
            self.stop_evt.wait(max(0.5, interval - (time.time() - t0)))
            return
        if _same_screen(sig, self.cont_sig):
            self.q.put(("status", "画面没变化，跳过"))
            self._cont_log("画面没变化，跳过（等下一轮）")
            self.stop_evt.wait(max(0.5, interval - (time.time() - t0)))
            return
        self.cont_sig = sig
        qno = qsplit.question_number(quick.text)
        if qno and qno[1]:
            self.q.put(("status", f"第 {qno[0]}/{qno[1]} 题 · 对比画面…"))

        # 同一道题（只是光标闪、计时器跳之类变了）就别再花钱解一遍
        same_no = bool(qno and self.cont_qno and qno[0] == self.cont_qno[0])
        if (self.cfg.get("dedupe", True) and self.last_problem
                and (same_no or _same_problem(sig, self.last_problem))):
            self.q.put(("status", "还是刚才那道题，跳过"))
            self._cont_log("同一道题，跳过（避免重复作答）")
            self.stop_evt.wait(max(0.5, interval - (time.time() - t0)))
            return
        self.last_problem = sig
        self.cont_qno = qno
        self._beep("new")

        self.q.put(("busy", True))
        cfg = self._current_cfg()
        try:
            payload = run_pipeline(
                str(tmp), cfg, lambda k, p: self.q.put((k, p)))
        except Exception:  # noqa: BLE001
            payload = {"ok": False, "stage": "crash",
                       "error": traceback.format_exc()[-900:]}
        self.q.put(("finish", payload))
        self.q.put(("busy", False))
        if self.stop_evt.is_set():
            return
        self.stop_evt.wait(max(0.6, interval - (time.time() - t0)))

    def _set_pulse(self, on: bool):
        if self._pulse_job:
            try:
                self.root.after_cancel(self._pulse_job)
            except Exception:  # noqa: BLE001
                pass
            self._pulse_job = None
        if not on:
            self.dot.configure(fg=UI["accent"])
            return
        self._pulse_state = True

        def tick():
            self._pulse_state = not getattr(self, "_pulse_state", True)
            try:
                self.dot.configure(
                    fg=UI["green"] if self._pulse_state else UI["bg"])
            except Exception:  # noqa: BLE001
                return
            self._pulse_job = self.root.after(520, tick)

        tick()

    # ------------------------------------------------------------ 事件循环
    def _reset_stream(self):
        self._stream_buf = ""
        self._stream_pending = 0

    def _on_stream(self, delta: str):
        """流式增量：先屯在缓冲里，每 ~120 字刷一次详情框，避免频繁重排。"""
        if not delta:
            return
        self._stream_buf = (getattr(self, "_stream_buf", "") + delta)[-4000:]
        self._stream_pending = getattr(self, "_stream_pending", 0) + len(delta)
        if self._stream_pending < 120 and "## 最终答案" not in self._stream_buf:
            return
        self._stream_pending = 0
        try:
            self.text.delete("1.0", "end")
            self.text.insert("end", "实时生成中…\n")
            self.text.insert("end", self._stream_buf[-1500:])
        except Exception:  # noqa: BLE001
            pass

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "balance":
                    try:
                        self._balance_text = f"\u00a5{payload['amount']:.2f}"
                        if payload.get("n"):
                            self._balance_text += f" \u00b7 \u7ea6 {payload['n']} \u9898"
                    except Exception:  # noqa: BLE001
                        self._balance_text = ""
                    self._refresh_status_meta()
                elif kind == "status":
                    self.set_status(str(payload))
                elif kind == "busy":
                    self.busy = bool(payload)
                    self._set_buttons(not self.busy)
                    if self.busy:
                        self._start_anim()
                    else:
                        self._stop_anim()
                elif kind == "finish":
                    self._finish(payload)
                elif kind == "warmup":
                    self._show_warmup(str(payload))
                elif kind == "update":
                    self._on_update_info(payload)
                elif kind == "followup":
                    self._show_followup(str(payload))
                elif kind == "stream":
                    self._on_stream(str(payload))
                elif kind == "judge":
                    self._show_judge(str(payload))
                elif kind == "alt":
                    self._show_alt(str(payload))
                elif kind == "practice":
                    self._show_practice(str(payload))
        except queue.Empty:
            pass

        if self.hk is not None:
            try:
                while True:
                    name = self.hk.events.get_nowait()
                    if name == "repeat":
                        self.repeat_region()
                    elif name == "region":
                        self.shot_region()
                    elif name == "full":
                        self.shot_full()
                    elif name == "redo":
                        self.redo()
                    elif name == "paste":
                        self.paste_image()
                    elif name == "next":
                        self.next_answer()
                    elif name == "follow":
                        self.ask_followup()
                    elif name == "text":
                        self.solve_text()
                    elif name == "mini":
                        self.toggle_mini()
            except queue.Empty:
                pass
            except Exception:  # noqa: BLE001
                pass

        self.root.after(110, self._poll)

    def _setup_hotkeys(self):
        try:
            import hotkey

            self.hk = hotkey.HotkeyManager()
            self.hk.add("region", self.cfg.get("hotkey_region", "ctrl+alt+q"),
                        ["ctrl+shift+q", "ctrl+alt+1"])
            # ctrl+alt+w 常被别的软件（截图工具/显卡驱动）占了，所以带退路
            self.hk.add("full", self.cfg.get("hotkey_full", "ctrl+alt+w"),
                        ["ctrl+alt+s", "ctrl+shift+w", "ctrl+alt+2"])
            # Ctrl+Alt+R：重算上一次（答案看着不对时不用回窗口点按钮）
            self.hk.add("redo", self.cfg.get("hotkey_redo", "ctrl+alt+r"),
                        ["ctrl+alt+e", "ctrl+shift+r"])
            # Ctrl+Alt+V：直接作答剪贴板里的图（截图工具存到剪贴板时，不用再框选）
            self.hk.add("paste", self.cfg.get("hotkey_paste", "ctrl+alt+v"),
                        ["ctrl+shift+v", "ctrl+alt+b"])
            # Ctrl+Alt+N：一屏多题时切到下一题的答案
            self.hk.add("next", self.cfg.get("hotkey_next", "ctrl+alt+n"),
                        ["ctrl+shift+n", "ctrl+alt+m"])
            # Ctrl+Alt+T：剪贴板里已经是题目文字 → 跳过 OCR 直接作答
            self.hk.add("text", self.cfg.get("hotkey_text", "ctrl+alt+t"),
                        ["ctrl+shift+t", "ctrl+alt+y"])
            # Ctrl+Alt+M：迷你答案条开关
            self.hk.add("follow", self.cfg.get("hotkey_follow", "ctrl+alt+f"),
                        ["ctrl+shift+f", "ctrl+alt+g"])
            self.hk.add("repeat", self.cfg.get("hotkey_repeat", "ctrl+alt+shift+q"),
                        ["ctrl+shift+alt+q", "ctrl+alt+shift+w"])
            self.hk.add("mini", self.cfg.get("hotkey_mini", "ctrl+alt+m"),
                        ["ctrl+shift+m", "ctrl+alt+k"])
            if not self.hk.start():
                self.log(f"⚠ 全局热键不可用：{self.hk.error}（按钮仍可用）")
            elif getattr(self.hk, "failed", None):
                self.log(f"⚠ 这些热键没注册上：{'、'.join(self.hk.failed)}")
            self._apply_hotkey_labels()
        except Exception as exc:  # noqa: BLE001
            self.log(f"⚠ 热键初始化失败：{exc}")

    def _key_name(self, name, default):
        spec = (getattr(self.hk, "bound", None) or {}).get(name, default)
        return "+".join(p.strip().capitalize() for p in spec.split("+"))

    def _apply_hotkey_labels(self):
        """把"实际生效的热键"显示出来（被占用时会自动换键，不能骗用户）。"""
        try:
            region = self._key_name("region", "ctrl+alt+q")
            self.hk_region_name = region
            if self.ans["fg"] == UI["dim"] or "圈住题目" in self.ans.cget("text"):
                self.ans.configure(text=f"按 {region} 圈住题目")
            # 状态栏只放最重要的两个键；其余在提示与 README 里（490px 放不下四条）
            self.hk_short = f"就绪 · {region} 框选 · Ctrl+Alt+T 文字作答"
            full = self._key_name("full", "ctrl+alt+w")
            want_full = (self.cfg.get("hotkey_full") or "ctrl+alt+w").lower()
            if (getattr(self.hk, "bound", None) or {}).get("full", want_full) != want_full:
                if not getattr(self, "_hk_noted", False):
                    self._hk_noted = True
                    self.log(f"ℹ {want_full.upper()} 被别的软件占用，整屏已改用 {full}")
            redo = self._key_name("redo", "ctrl+alt+r")
            paste = self._key_name("paste", "ctrl+alt+v")
            self.log(f"热键：框选 {region} · 整屏 {full} · 重算 {redo} · 剪贴板图 {paste} · 纯文字 Ctrl+Alt+T · 迷你 Ctrl+Alt+M")
            self.set_status(self.hk_short)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 结果渲染
    def _finish(self, payload):
        self.busy = False
        self._anim = 0
        self._anim_job = None
        self._status_base = "就绪"
        # 窗口被最小化时也要让答案现身（连续模式下人在看别处）
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
                self.root.after(60, lambda: snap_bottom_right(self.root))
        except Exception:  # noqa: BLE001
            pass
        self._set_buttons(True)
        if not self.cont_on:
            self._set_pulse(False)

        if not payload.get("ok"):
            self.ans.configure(text="这一次没成功", fg=UI["red"])
            self.chip.configure(text="⚠ 出错", bg=UI["red_bg"], fg=UI["red"])
            self.edge.configure(bg=UI["red"])
            self._set_agree(0.0, UI["red"])
            self.meta.configure(text=str(payload.get("stage", "")))
            self._flash(UI["red"])
            self.text.delete("1.0", "end")
            self.text.insert("end", "❌ 失败：\n", "err")
            self.text.insert("end", str(payload.get("error")) + "\n")
            self.set_status(prompts.MSG_RETRY_STATUS)
            self._enable_retry()
            self._beep("warn")
            return

        res = payload["result"]
        if res.answer.startswith("（调用失败）"):
            self.ans.configure(text="DeepSeek 调用失败", fg=UI["red"])
            self.chip.configure(text="🔴 未作答", bg=UI["red_bg"], fg=UI["red"])
            self.edge.configure(bg=UI["red"])
            self._set_agree(0.0, UI["red"])
            self.meta.configure(text=str(res.error)[:60])
            self._flash(UI["red"])
            self.text.delete("1.0", "end")
            self.text.insert("end", f"❌ {res.error}\n", "err")
            self.set_status(prompts.MSG_RETRY_STATUS)
            self._enable_retry()
            self._beep("warn")
            return

        self.last_payload = payload
        self._failed = False
        self.b_copy.set_enabled(True)
        self.b_redo.set_text(prompts.L("redo"))
        self.b_redo.set_enabled(True)
        items = payload.get("items") or [res]
        self.multi_items = items
        self.multi_idx = 0
        self.b_steps.set_enabled(bool(res.reasoning))
        self.b_bad.set_enabled(True)
        self.b_judge.set_enabled(True)
        self.b_practice.set_enabled(True)
        self.b_card.set_enabled(True)
        try:
            self.b_latex.set_enabled(bool(getattr(res, "latex", "")))
            self.b_alt.set_enabled(True)
        except Exception:  # noqa: BLE001
            pass
        self.b_speak.set_enabled(True)
        try:
            _expr = plotter.extract_function(payload.get("text") or "")
            self.b_plot.set_enabled(bool(_expr) and bool(self.cfg.get("plot", True)))
        except Exception:  # noqa: BLE001
            self.b_plot.set_enabled(False)
        # 存一份题目截图，导出 PDF 时一起排进去
        try:
            src = getattr(self, "last_image_path", "")
            if src and Path(src).exists():
                shots = ROOT / "shots"
                shots.mkdir(exist_ok=True)
                dst = shots / (Path(src).stem + ".png")
                if not dst.exists():
                    shutil.copyfile(src, dst)
                self.shot_path = str(dst)
                if self.shot_path not in self.recent_shots:
                    self.recent_shots.append(self.shot_path)
                    self.recent_shots = self.recent_shots[-12:]
        except Exception:  # noqa: BLE001
            self.shot_path = getattr(self, "last_image_path", "")
        self._task_t0 = 0.0
        self._slow_warned = False
        try:
            self._note_answer_consistency(qn2, res)
        except Exception:  # noqa: BLE001
            pass
        label, key = CONF_CHIP.get(res.confidence, ("⚪ " + res.confidence, "muted"))
        head = res.answer
        if len(items) > 1:
            head = f"第 1/{len(items)} 题：{res.answer}"
        self.ans.configure(text=head, fg=UI["text"])
        self.chip.configure(text=label, fg=UI.get(key, UI["muted"]),
                            bg=UI.get(key + "_bg", UI["card2"]))
        self.edge.configure(bg=UI.get(key, UI["accent"]))
        self._set_agree(res.agreement, UI.get(key, UI["accent"]))
        # 题面（可改重算）+ 英文翻译，存进「题面」条
        try:
            self.set_excerpt(payload.get("pretty_q") or latexfmt.pretty(payload.get("text") or ""),
                             payload.get("translation") or "")
            if payload.get("translation") and not getattr(self, "ex_visible", False):
                self.toggle_excerpt(True)     # 有译文时自动展开，省一次点击
        except Exception:  # noqa: BLE001
            pass
        self._today_count = (getattr(self, "_today_count", 0) or 0) + 1
        if not hasattr(self, "_today_cost"):
            self._today_cost = 0.0
        bits = [f"一致 {res.agreement:.0%}",
                f"{sum(i.calls for i in items)} 次",
                f"{payload['elapsed']:.1f}s",
                BACKEND_LABEL.get(str(payload.get("backend", "")),
                                  str(payload.get("backend", "")))]
        if len(items) > 1:
            bits.insert(0, f"共 {len(items)} 题")
        if getattr(res, "r1", False):
            bits.insert(0, "R1 复核")
        if getattr(res, "dual", False):
            agree = "一致" in (getattr(res, "dual_note", "") or "")
            bits.insert(0, "双模型" + ("✓" if agree else "✗"))
        if payload.get("cached"):
            bits.insert(0, "⚡ 缓存")
        tin = sum(int(getattr(i, "tokens_in", 0) or 0) for i in items)
        tout = sum(int(getattr(i, "tokens_out", 0) or 0) for i in items)
        if tin or tout:
            bits.append("¥{0:.4f}".format(cfg_mod.cost_of(tin, tout, self.cfg)))
        chk = getattr(res, "check", "") or ""
        if chk == "ok":
            bits.insert(0, "✅ 代入验算")
        elif chk == "fail":
            bits.insert(0, "⚠ 代入验算不通过")
        self.meta.configure(text=" · ".join(bits))
        # 预算/余额提醒（每题只看一次，别每题都跳）
        try:
            budget = float(self.cfg.get("budget_day", 1.0) or 0)
            if budget > 0 and not self._budget_warned:
                st = cfg_mod.daily_stats(self.cfg)
                if float(st.get("cost", 0)) > budget:
                    self._budget_warned = True
                    self.note(prompts.MSG_BUDGET_LOW.format(
                        cost=float(st["cost"]), budget=budget))
                    self.chip.configure(bg=UI["amber_bg"], fg=UI["amber"])
        except Exception:  # noqa: BLE001
            pass
        self._flash(UI.get(key, UI["accent"]))
        if self.mini is not None:
            try:
                self.mini.set_text(f"{res.fill or res.answer}", key)
            except Exception:  # noqa: BLE001
                pass

        self.text.delete("1.0", "end")
        for w in payload.get("warnings") or []:
            self.text.insert("end", f"⚠ {w}\n", "warn")
        if len(items) > 1:
            self.text.insert("end", "各题答案\n", "h")
            for i, r in enumerate(items, 1):
                extra = f"   → 填入：{r.fill}" if r.fill else ""
                self.text.insert("end", f"{i}. {r.answer}{extra}\n", "answer")
            self.text.insert("end", "第 1 题推理\n", "h")
        else:
            if res.fill:
                self.text.insert("end", f"填入答案：{res.fill}\n", "answer")
            self.text.insert("end", "推理\n", "h")
        self.text.insert("end", pretty_reasoning(res.reasoning) + "\n")
        if len(res.samples) > 1:
            self.text.insert("end", "各次采样\n", "h")
            for i, s in enumerate(res.samples, 1):
                self.text.insert("end", f"{i}. {s}\n", "muted")
        self.text.insert("end", "识别到的原文\n", "h")
        self.text.insert("end", payload["text"] + "\n", "muted")
        self.text.see("1.0")

        # 自动复制：只要拿到答案就进剪贴板（用户要求），不看置信度
        copied = False
        to_copy = res.fill or res.answer
        if self.cfg.get("auto_copy") and to_copy and "调用失败" not in to_copy:
            copied = self._copy_text(to_copy, quiet=True)
        blanks = [b.strip() for b in re.split(r"[,，;；]", res.fill) if b.strip()] \
            if res.fill else []
        if len(items) == 1 and len(blanks) > 1:
            self._render_blanks(blanks)
        else:
            self._render_multi(items)
        try:
            if len(items) > 1:
                for i, r in enumerate(items, 1):
                    self._remember(f"{i}题", r.fill or r.answer)
            else:
                qn = qsplit.question_number(payload.get("text") or "")
                self._remember(f"第{qn[0]}题" if qn and qn[0] else "上题", to_copy)
        except Exception:  # noqa: BLE001
            pass
        if (copied and self.cfg.get("refocus", True) and not self.cont_on
                and self.prev_hwnd):
            self.root.after(90, lambda: capture.restore_focus(self.prev_hwnd))
        # 自动粘贴：把答案直接敲进刚才那个输入框（可选再回车提交）
        if copied and self.cfg.get("auto_paste") and not self.cont_on and to_copy:
            hwnd = self.prev_hwnd
            enter = bool(self.cfg.get("auto_enter"))
            if hwnd:
                self.root.after(130, lambda: capture.restore_focus(hwnd))
            self.root.after(330, lambda: capture.send_paste(enter=enter))
        tail = "  ·  📋 已自动复制" if copied else ""
        qno = qsplit.question_number(payload.get("text") or "")
        qtag = (f"第 {qno[0]}/{qno[1]} 题 · " if qno and qno[1]
                else (f"第 {qno[0]} 题 · " if qno else ""))
        mode = " · 极速" if self.cfg.get("fast") else ""
        self.set_status(f"{qtag}{label}{mode} · 总 {payload['elapsed']:.1f}s{tail}")
        self._beep("done")
        if self.cfg.get("tts"):
            self.speak_answer()
        try:
            cfg_mod.save_history(
                payload["text"], format_answer(payload),
                f"reader={payload.get('backend', '')} "
                f"model={MODEL_LABEL2ID.get(self.model_var.get(), 'deepseek-chat')}"
                f" conf={res.confidence}")
        except Exception:  # noqa: BLE001
            pass
        # 历史面板 / CSV 用的结构化记录
        try:
            qn2 = qsplit.question_number(payload.get("text") or "")
            cfg_mod.add_record({
                "title": (f"第{qn2[0]}/{qn2[1]}题" if qn2 and qn2[1]
                          else (f"第{qn2[0]}题" if qn2 else "题目")),
                "answer": res.answer, "fill": res.fill,
                "confidence": res.confidence, "model": self.cfg.get("model", ""),
                "elapsed": round(float(payload.get("elapsed", 0)), 2),
                "calls": sum(i.calls for i in items),
                "backend": payload.get("backend", ""),
                "cached": bool(payload.get("cached")),
                "question": (payload.get("text") or "")[:800],
                "shot": getattr(self, "shot_path", ""),
                "tokens_in": sum(int(getattr(i, "tokens_in", 0) or 0) for i in items),
                "tokens_out": sum(int(getattr(i, "tokens_out", 0) or 0) for i in items),
                "cost": round(cfg_mod.cost_of(
                    sum(int(getattr(i, "tokens_in", 0) or 0) for i in items),
                    sum(int(getattr(i, "tokens_out", 0) or 0) for i in items),
                    self.cfg), 5),
                "check": getattr(res, "check", ""),
            })
        except Exception:  # noqa: BLE001
            pass

    def selfcheck(self):
        """一键自检：Key / 余额 / OCR / 视觉后端 / 热键（写进详情框，不花钱）。"""
        lines = []
        key = str(self.cfg.get("api_key") or "")
        lines.append(("\u2705" if key.startswith("sk-") and len(key) > 20 else "\u274c")
                     + f" API Key\uff1a\u5c3e\u53f7 {key[-4:]}" if key else "\u274c API Key\uff1a\u672a\u586b")
        try:
            bal = DeepSeekClient(key, self.cfg.get("base_url", cfg_mod.DEFAULTS["base_url"]),
                                 self.cfg.get("model", cfg_mod.DEFAULTS["model"])).balance()
            if bal.get("ok"):
                lines.append(f"\u2705 \u4f59\u989d\uff1a{bal.get('amount')} {bal.get('currency')}")
            else:
                lines.append(f"\u26a0 \u4f59\u989d\u67e5\u8be2\u5931\u8d25\uff1a{bal.get('error')}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\u26a0 \u4f59\u989d\u67e5\u8be2\u5f02\u5e38\uff1a{exc}")
        try:
            import psocr
            ok = psocr.available()
            lines.append(("\u2705" if ok else "\u274c") + " \u672c\u5730 OCR\uff08Windows.Media.Ocr\uff09")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\u274c \u672c\u5730 OCR\uff1a{exc}")
        try:
            st = reader.backend_status()
            mv = st.get("modlens") or {}
            lines.append(("\u2705 \u89c6\u89c9\uff1a" if mv.get("available") else "\u26a0 \u89c6\u89c9\u4e0d\u53ef\u7528\uff1a")
                         + str(mv.get("detail") or "")[:60])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\u26a0 \u89c6\u89c9\u68c0\u6d4b\u5f02\u5e38\uff1a{exc}")
        try:
            bound = ", ".join(f"{k}={v}" for k, v in (self.hk.bound or {}).items())
            lines.append(f"\u2328 \u70ed\u952e\uff1a{bound or '\u672a\u6ce8\u518c'}")
        except Exception:  # noqa: BLE001
            pass
        st = cfg_mod.daily_stats(self.cfg)
        lines.append(f"\U0001f4b0 \u4eca\u65e5\uff1a{st.get('count')} \u9898\u00b7\u00a5{float(st.get('cost') or 0):.4f}")
        text = "\n".join(lines)
        self._set_detail("\u25ae \u81ea\u68c0\n" + text)
        try:
            messagebox.showinfo("\u81ea\u68c0", text[:900])
        except Exception:  # noqa: BLE001
            pass
        return text

    def _vision_selftest(self) -> str:
        """跑一次真实的视觉识别，把成功/失败原因原样报给用户。"""
        try:
            import reader
            from PIL import Image, ImageDraw, ImageFont
            tmp = Path(tempfile.gettempdir()) / "screenqa_vision_selftest.png"
            img = Image.new("RGB", (720, 220), "#ffffff")
            d = ImageDraw.Draw(img)
            f1 = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
            d.text((24, 22), "Solve for x:", font=f1, fill="#111111")
            d.text((24, 80), "3x + 7 = 25", font=f1, fill="#111111")
            d.text((24, 150), "测试用图 VISION-SELFTEST", font=f1, fill="#333333")
            img.save(tmp)

            timeout_s = int(self.cfg.get("modlens_timeout", 120))
            res = reader.read_with_modlens(str(tmp), timeout_s=timeout_s)
            if res.error:
                hint = ""
                if "EPERM" in res.error or "EINVAL" in res.error:
                    hint = ("\n\n可能原因：CLI 启动被系统拒绝。本程序已把 "
                            "tools/cli_shims（真 .exe 垫片）放进 PATH；若仍失败，"
                            "请确认 codex / claude CLI 已登录。")
                return f"❌ 视觉模型不可用\n{res.error}{hint}"
            text = (res.text or "").strip()
            if not text:
                return "❌ 视觉模型返回了空文本（模型能启动，但没读出内容）"
            return f"✅ 视觉可用（{res.elapsed:.1f}s，{len(text)} 字）\n{text[:400]}"
        except Exception as exc:
            return f"❌ 视觉自检异常：{type(exc).__name__}: {exc}"

    def _enable_retry(self):
        self._failed = True
        try:
            if self.last_job:
                kind, value = self.last_job
                n = cfg_mod.queue_add({"kind": kind, "value": str(value)})
                if n:
                    self.note(prompts.MSG_QUEUE_ADDED + f"（共 {n} 条）")
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.last_job:
                self.b_redo.set_text(prompts.LBL_RETRY)
                self.b_redo.set_enabled(True)
            else:
                self.b_redo.set_enabled(False)
        except Exception:  # noqa: BLE001
            pass

    def retry(self):
        """失败后一键重试：图片就原图重跑一次，文字就原文字重跑一次。"""
        if self.busy or not self.last_job:
            return
        kind, val = self.last_job
        self.set_status(prompts.MSG_RETRY_RUN)
        if kind == "text":
            self._start_text(val)
        else:
            self._run_path(val, bypass=False)

    def redo(self):
        """失败 → 重试；成功 → 重算（跳过缓存，多问几路）。"""
        if self.busy:
            return
        if self._failed:
            return self.retry()
        if not self.last_image:
            return
        self.set_status("重算中（跳过缓存）…")
        self._run_path(self.last_image, bypass=True)

    def warmup(self):
        """考前热身：查一遍环境，再用两张真实题图各跑一次（samples=1，省钱）。"""
        if self.busy:
            return
        self.busy = True
        self._set_buttons(False)
        self.set_status("热身中…")
        self._set_pulse(True)
        cfg = dict(self._current_cfg())
        cfg.update({"samples": 1, "verify": False, "escalate": False,
                    "cache": False, "multi": False, "dual": False,
                    "reasoner": False, "sound": False, "auto_copy": False})

        def emit(kind, payload):
            self.q.put((kind, payload))

        def work():
            lines = [prompts.WARMUP_HEAD]
            ok_n = 0
            cases = [("c01_frac", "cases/c01_frac.png"),
                     ("c04_ineq", "cases/c04_ineq.png")]
            try:
                key = str(cfg.get("api_key") or "")
                if len(key) > 12 and key.startswith("sk-"):
                    lines.append(prompts.WARMUP_KEY_OK.format(tail=key[-4:]))
                else:
                    lines.append(prompts.WARMUP_KEY_BAD)
                try:
                    cli = DeepSeekClient(api_key=key,
                                         base_url=cfg.get("base_url", "https://api.deepseek.com"),
                                         model=cfg.get("model", "deepseek-chat"))
                    bal = cli.balance(timeout=10)
                    if bal.get("ok"):
                        lines.append(prompts.WARMUP_BAL.format(
                            text=f"{bal.get('amount')} {bal.get('currency')}"))
                    else:
                        lines.append(prompts.WARMUP_BAL.format(text="⚠ 查询失败"))
                except Exception as exc:  # noqa: BLE001
                    lines.append(prompts.WARMUP_BAL.format(text=f"⚠ {exc}"))
                try:
                    import psocr
                    lines.append(prompts.WARMUP_OCR.format(
                        text="✅ 可用" if psocr.available() else "❌ 不可用"))
                except Exception as exc:  # noqa: BLE001
                    lines.append(prompts.WARMUP_OCR.format(text=f"❌ {exc}"))
                try:
                    st = reader.backend_status()
                    mv = st.get("modlens") or {}
                    lines.append(prompts.WARMUP_VIS.format(
                        text="✅ 找到 modlens" if mv.get("available") else "⚠ 不可用（会自动降级 OCR）"))
                except Exception as exc:  # noqa: BLE001
                    lines.append(prompts.WARMUP_VIS.format(text=f"⚠ {exc}"))
                try:
                    bound = getattr(self.hk, "bound", {}) or {}
                    lines.append(prompts.WARMUP_HOTKEY.format(
                        text="、".join(f"{k}={v}" for k, v in bound.items()) or "⚠ 没注册上"))
                except Exception:  # noqa: BLE001
                    lines.append(prompts.WARMUP_HOTKEY.format(text="⚠ 未知"))
                for name, rel in cases:
                    pth = ROOT / rel
                    if not pth.exists():
                        lines.append(prompts.WARMUP_CASE.format(name=name, text="⚠ 缺图，跳过"))
                        continue
                    t0 = time.time()
                    out = run_pipeline(str(pth), cfg, emit)
                    if out.get("ok"):
                        res = out["result"]
                        ok_n += 1
                        lines.append(prompts.WARMUP_CASE.format(
                            name=name,
                            text=f"✅ {str(res.answer)[:28]}（{time.time()-t0:.1f}s）"))
                    else:
                        lines.append(prompts.WARMUP_CASE.format(
                            name=name, text=f"❌ {str(out.get('error'))[:40]}"))
            except Exception:  # noqa: BLE001
                lines.append("\n（热身过程中出错）\n" + traceback.format_exc()[-500:])
            finally:
                bad = len(cases) - ok_n
                if ok_n == len(cases):
                    verdict = prompts.WARMUP_PASS.format(ok=ok_n, n=len(cases))
                elif ok_n > 0:
                    verdict = prompts.WARMUP_PART.format(bad=bad)
                else:
                    verdict = prompts.WARMUP_FAIL
                self.q.put(("warmup", "".join(lines) + prompts.WARMUP_TAIL.format(verdict=verdict)))

        threading.Thread(target=work, daemon=True).start()

    def _show_warmup(self, text: str):
        self.busy = False
        self._set_buttons(True)
        self._set_pulse(False)
        self.set_status("热身完成")
        try:
            self.text.delete("1.0", "end")
            self.text.insert("end", text)
        except Exception:  # noqa: BLE001
            pass
        self.log("考前热身完成")
        try:
            messagebox.showinfo(prompts.WARMUP_TITLE, text[:1500], parent=self.root)
        except Exception:  # noqa: BLE001
            pass

    def _hot_copy(self, idx: int):
        """数字键 1-9：复制第 N 题的答案（多题时很省事）。"""
        items = getattr(self, "multi_items", None) or []
        if not items:
            return
        if idx >= len(items):
            self.set_status(f"只有 {len(items)} 题 · 按 1-{min(9, len(items))}")
            return
        self.multi_idx = idx
        self._copy_index(idx, items[idx].fill or items[idx].answer)

    def _render_multi(self, items):
        """一屏多题时，给每题一个「复制」按钮（最多 8 题）+ 一个「下一题」。"""
        try:
            for w in self.multi_bar.winfo_children():
                w.destroy()
            if len(items) < 2:
                return
            Pill(self.multi_bar, "下一题 ▶", self.next_answer, kind="flat",
                 height=28, padx=12, font_size=9).pack(side="right")
            for i, r in enumerate(items[:8], 1):
                r = _item_res(r)
                val = r.fill or r.answer
                label = f"{i}·{val[:9]}"
                Pill(self.multi_bar, label,
                     (lambda v=val, idx=i: self._copy_index(idx, v)),
                     kind="flat", height=28, padx=12, font_size=9
                     ).pack(side="left", padx=(0, 6))
        except Exception:  # noqa: BLE001
            pass

    def _copy_index(self, idx: int, value: str):
        """点某一题的按钮：复制它，并把它记为"当前第几题"。"""
        self.multi_idx = max(0, int(idx) - 1)
        if self._copy_text(value):
            try:
                self.set_status(f"第 {idx} 题答案已复制")
            except Exception:  # noqa: BLE001
                pass

    def next_answer(self):
        """多题连答：切到下一题的答案（Ctrl+Alt+N 或点「下一题」）。"""
        items = self.multi_items or []
        if len(items) < 2:
            self.set_status("这一屏只有一道题，不需要下一题")
            return
        self.multi_idx = (self.multi_idx + 1) % len(items)
        r = _item_res(items[self.multi_idx])
        val = r.fill or r.answer
        if self._copy_text(val):
            self.set_status(f"第 {self.multi_idx + 1}/{len(items)} 题答案已复制")
            self._flash(UI["accent"])
            self._beep("done")
            if self.cfg.get("auto_paste") and self.prev_hwnd:
                hwnd = self.prev_hwnd
                enter = bool(self.cfg.get("auto_enter"))
                self.root.after(80, lambda: capture.restore_focus(hwnd))
                self.root.after(240, lambda: capture.send_paste(enter=enter))

    def _remember(self, label: str, value: str):
        """记一条最近的答案（内存里留 6 条，够考试里来回翻）。"""
        if not value:
            return
        try:
            self.history = [h for h in getattr(self, "history", [])
                            if h.get("value") != value][:5]
            self.history.insert(0, {"label": label, "value": value})
            self._render_history()
        except Exception:  # noqa: BLE001
            pass

    def _render_history(self):
        try:
            for w in self.hist_bar.winfo_children():
                w.destroy()
            hist = getattr(self, "history", [])
            if not hist:
                return
            tk.Label(self.hist_bar, text="最近", bg=self.hist_bar.cget("bg"), fg=UI["muted"],
                     font=(FONT, 8)).pack(side="left", padx=(2, 6))
            for h in hist[:6]:
                Pill(self.hist_bar, f"{h['label']}·{h['value'][:7]}",
                     (lambda v=h["value"]: self._copy_text(v)),
                     kind="flat", height=26, padx=10, font_size=8
                     ).pack(side="left", padx=(0, 5))
        except Exception:  # noqa: BLE001
            pass

    def _render_blanks(self, blanks):
        """多个空（MyMathLab 常见）→ 每个空一个复制按钮。"""
        try:
            for w in self.multi_bar.winfo_children():
                w.destroy()
            for i, b in enumerate(blanks[:8], 1):
                Pill(self.multi_bar, f"空{i}·{b[:9]}",
                     (lambda v=b: self._copy_text(v)),
                     kind="flat", height=28, padx=12, font_size=9
                     ).pack(side="left", padx=(0, 6))
        except Exception:  # noqa: BLE001
            pass

    def _set_detail(self, text: str):
        try:
            self.det.configure(state="normal")
            self.det.delete("1.0", "end")
            self.det.insert("1.0", text or "")
            self.det.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _copy_text(self, text: str, quiet=False) -> bool:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            if not quiet:
                self.set_status("答案已复制")
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"复制失败：{exc}")
            return False

    def copy_answer(self):
        if not self.last_payload:
            return
        res = self.last_payload["result"]
        ok = self._copy_text(res.fill or res.answer)
        if ok:
            self._copy_flash()

    def copy_steps(self):
        """只复制推理步骤（对答案要做人工核对时用）。"""
        if not self.last_payload:
            return
        res = self.last_payload["result"]
        body = pretty_reasoning(res.reasoning or "").strip()
        if not body:
            self.set_status("这次没有推理内容可复制")
            return
        head = ""
        try:
            head = f"题目：\n{(self.last_payload.get('text') or '')[:600]}\n\n"
        except Exception:  # noqa: BLE001
            head = ""
        ok = self._copy_text(head + "解题步骤：\n" + body)
        if ok:
            self.set_status("解题步骤已复制")

    def mark_error(self):
        """把当前这题标记为"答错了"：下次遇到同一题自动多问两路。"""
        if not self.last_payload:
            return
        payload = self.last_payload
        res = payload.get("result")
        key = payload.get("key") or _cache_key(payload.get("text") or "", self.cfg)
        try:
            total = cfg_mod.add_error(key, payload.get("text") or "",
                                      (res.fill or res.answer) if res else "")
            self.set_status(f"已记入错题本（共 {total} 题）· 下次遇到会多问 2 路")
            self._beep("warn")
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"记录失败：{exc}")

    # ------------------------------------------------------------ 历史 / 更新
    # ------------------------------------------------------------ 最近图
    def install_update(self):
        """把 _update 里的内容覆盖到程序目录（保留 config.json / history / cache）。"""
        upd = ROOT / "_update"
        if not upd.exists():
            messagebox.showinfo("安装更新", "还没有下载过更新，先点「下载更新」。")
            return
        if not messagebox.askyesno(
                "安装更新",
                f"会用 {upd} 里的文件覆盖当前程序（config.json、history、缓存都保留）。\n"
                "覆盖后需要重开程序。继续？"):
            return
        if getattr(sys, "frozen", False):
            # 打包版：正在运行的 exe 不能被覆盖，交给批处理「等本进程退出→替换→重启」
            info = extras.apply_update_now()
            if not info.get("ok"):
                messagebox.showerror("安装更新", f"失败：{info.get('error')}")
                return
            messagebox.showinfo("安装更新",
                                "已安排替换：程序会关闭，2 秒后自动覆盖并重新启动。")
            self._quit()
            return
        try:
            info = extras.apply_update(keep=("config.json", "history", "_tmp", "_app.log"))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("安装更新", f"失败：{exc}")
            return
        self.log(f"已覆盖 {info.get('copied', 0)} 个文件"
                 + (f"，跳过 {info.get('skipped', 0)} 个" if info.get("skipped") else ""))
        messagebox.showinfo("安装更新",
                            f"已更新 {info.get('copied', 0)} 个文件。\n重开程序即可用新版本。")

    def download_update(self):
        """Download latest source from GitHub into <app dir>/_update (no auto-replace)."""
        if not messagebox.askyesno(
                "下载更新",
                "会把 GitHub 最新源码下载到程序目录的 _update 文件夹（不会自动覆盖当前程序）。继续？"):
            return
        self.log("开始下载更新…")
        self.set_status("下载更新 …")

        def worker():
            try:
                info = extras.prepare_update()
            except Exception as exc:  # noqa: BLE001
                info = {"ok": False, "error": str(exc)}
            if info.get("ok"):
                self.log("下载完成：%s（%d 个文件）" % (info.get("path"), info.get("files", 0)))
                msg = ("已下载到：\n%s\n\n替换方法：退出本程序 → 把 _update 里的文件"
                       "复制覆盖到程序目录 → 重新打开。" % info.get("path"))
                self.root.after(0, lambda: messagebox.showinfo("下载完成", msg))
            else:
                self.log("下载失败：%s" % info.get("error"))
                err = str(info.get("error"))
                self.root.after(0, lambda: messagebox.showwarning("下载失败", err))

        threading.Thread(target=worker, daemon=True).start()

    def export_cards(self):
        try:
            n, path = cfg_mod.export_anki()
            self.log("已导出 %d 张错题卡片：%s" % (n, path))
            messagebox.showinfo("导出卡片",
                                "已导出 %d 张错题卡片：\n%s\n\n"
                                "Anki → 文件 → 导入 → 选这个 txt" % (n, path))
        except Exception as exc:  # noqa: BLE001
            self.log("导出卡片失败：%s" % exc)



    # ------------------------------------------------------------ v2.13 输入与工具
    def ask_typed(self):
        """弹窗手敲题目（完全绕开 OCR）。"""
        win = tk.Toplevel(self.root)
        win.title(prompts.MSG_TYPED_TITLE)
        win.configure(bg=UI["card"])
        win.geometry("560x360")
        win.transient(self.root)
        try:
            apply_icon(win)
        except Exception:  # noqa: BLE001
            pass
        tk.Label(win, text=prompts.MSG_TYPED_TITLE, bg=UI["card"], fg=UI["dim"],
                 font=(FONT, 10), anchor="w").pack(fill="x", padx=14, pady=(12, 6))
        box = tk.Text(win, height=10, bg=UI["card2"], fg=UI["text"], bd=0,
                      insertbackground=UI["accent"], font=(FONT, 11),
                      padx=10, pady=8, wrap="word")
        box.pack(fill="both", expand=True, padx=14)
        box.focus_set()

        def submit(_ev=None):
            text = box.get("1.0", "end").strip()
            if len(text) < 6:
                messagebox.showinfo("提示", prompts.MSG_TYPED_EMPTY, parent=win)
                return
            win.destroy()
            self._start_text(text)

        row = tk.Frame(win, bg=UI["card"])
        row.pack(fill="x", padx=14, pady=10)
        Pill(row, prompts.L("cancel"), win.destroy, kind="ghost", width=64).pack(side="right")
        Pill(row, prompts.L("ok"), submit, kind="primary", width=76).pack(side="right", padx=(0, 8))
        box.bind("<Control-Return>", submit)
        win.bind("<Escape>", lambda _e: win.destroy())

    def solve_typed(self):
        self.ask_typed()

    def open_image(self):
        path = filedialog.askopenfilename(
            title="打开题目图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp"), ("所有文件", "*.*")])
        if path:
            self._run_path(path)

    def copy_latex(self):
        payload = self.last_payload or {}
        latex = ""
        try:
            latex = (payload.get("result").latex or "") if payload.get("result") else ""
        except Exception:  # noqa: BLE001
            latex = ""
        if not latex:
            self.set_status(prompts.MSG_LATEX_NONE)
            return
        if self._copy_text(latex):
            self._copy_flash()
            self.set_status(prompts.MSG_LATEX_COPIED)

    def alt_method(self):
        payload = self.last_payload or {}
        if not payload:
            self.set_status(prompts.MSG_ALT_NONE)
            return
        self.set_status(prompts.MSG_ALT_RUN)
        cfg = self._current_cfg()
        question = payload.get("text") or ""
        result = payload.get("result")

        def worker():
            try:
                client = deepseek_api.DeepSeekClient(
                    cfg["api_key"], cfg["base_url"], cfg["model"], timeout=90)
                out = solver.alt_method(
                    client, question,
                    getattr(result, "answer", "") if result else "",
                    getattr(result, "reasoning", "") if result else "",
                    model=cfg["model"])
                self.q.put(("alt", out))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("alt", f"换解法失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _show_alt(self, text):
        try:
            self._set_detail(prompts.LBL_ALT_TITLE + "\n\n" + (text or ""))
        except Exception:  # noqa: BLE001
            pass
        self.set_status("已换一种解法，详见下方")

    def retry_queue(self):
        item = cfg_mod.queue_pop()
        if not item:
            self.set_status(prompts.MSG_QUEUE_EMPTY)
            return
        self.set_status(prompts.MSG_QUEUE_RUN)
        kind, value = item.get("kind"), item.get("value")
        if kind == "text":
            self._start_text(str(value))
        else:
            self._run_path(str(value))

    def recent_menu(self):
        if not self.recent_shots:
            self.log("还没有做过的题图")
            return
        menu = tk.Menu(self.root, tearoff=0, bg=UI["card"], fg=UI["text"],
                       activebackground=UI["accent"], activeforeground="#07121f",
                       borderwidth=0, font=(FONT, 9))
        for item in reversed(self.recent_shots[-6:]):
            label = Path(item).stem[-24:]
            menu.add_command(label=label, command=lambda p=item: self._run_path(p))
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    # ------------------------------------------------------------ 区域记忆
    def region_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg=UI["card"], fg=UI["text"],
                       activebackground=UI["accent"], activeforeground="#07121f",
                       borderwidth=0, font=(FONT, 9))
        regions = self.cfg.get("regions") or []
        for r in regions:
            if not isinstance(r, dict) or not r.get("bbox"):
                continue
            name = str(r.get("name") or "区域")
            menu.add_command(label=name + "  " + str(r["bbox"]),
                             command=lambda rr=r: self._use_region(rr))
        if regions:
            menu.add_separator()
        menu.add_command(label="圈选新区域…", command=lambda: self.pick_region(quiet=True))
        menu.add_command(label="记住当前区域…", command=self._save_region)
        if regions:
            menu.add_command(label="清除记住的区域", command=self._clear_regions)
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _use_region(self, region):
        try:
            bbox = list(region.get("bbox") or [])
            if len(bbox) != 4:
                return
            self.cont_region = tuple(int(v) for v in bbox)
            self.cfg["cont_bbox"] = list(self.cont_region)
            cfg_mod.save(self.cfg)
            self.region_lbl.configure(text=self._region_text())
            self.log("区域切到：%s" % region.get("name"))
        except Exception as exc:  # noqa: BLE001
            self.log("切换区域失败：%s" % exc)

    def _save_region(self):
        if not self.cont_region:
            self.log("当前是整屏；先圈一个区域再记住")
            return
        name = "区域 %d" % ((len(self.cfg.get("regions") or [])) + 1)
        try:
            self.cfg["regions"] = cfg_mod.add_region(name, self.cont_region)
            self.log(prompts.MSG_REGION_SAVED.format(name=name))
        except Exception as exc:  # noqa: BLE001
            self.log("记住区域失败：%s" % exc)

    def _clear_regions(self):
        self.cfg["regions"] = []
        try:
            saved = cfg_mod.load()
            saved["regions"] = []
            cfg_mod.save(saved)
        except Exception:  # noqa: BLE001
            pass
        self.log("已清除记住的区域")

    # ------------------------------------------------------------ 追问
    def ask_followup(self):
        prev = self.last_payload or {}
        res = prev.get("result")
        if res is None:
            self.log(prompts.MSG_NEED_PREV)
            self._beep("warn")
            return
        self.log(prompts.MSG_FOLLOWUP_HINT)
        sel = capture.RegionSelector(self.root)
        img = sel.select()
        if img is None:
            return
        path = self._shot_path()
        try:
            img.save(path)
        except Exception as exc:  # noqa: BLE001
            self.log("save failed: %s" % exc)
            return
        self._set_buttons(False)
        self.set_status(prompts.LBL_FOLLOWUP + " ...")

        def worker():
            try:
                out = run_followup(path, self.cfg, self._emit_threadsafe,
                                   prev.get("text") or "", res.answer)
            except Exception as exc:  # noqa: BLE001
                out = {"ok": False, "error": str(exc), "elapsed": 0.0}
            self.q.put(("followup", out))

        threading.Thread(target=worker, daemon=True).start()

    def _show_followup(self, out):
        self._set_buttons(True)
        if not out.get("ok"):
            self.set_status("追问失败")
            self.log("⚠ follow-up failed: %s" % out.get("error"))
            self._beep("warn")
            return
        self.set_status(prompts.MSG_FOLLOWUP_TITLE.format(sec=float(out.get("elapsed") or 0)))
        self.text.insert("1.0", "\n\n" + (out.get("text") or ""))
        self._beep("done")

    # ------------------------------------------------------------ 一致性
    def _note_answer_consistency(self, qno, res):
        if not qno:
            return
        no = qno[0]
        prev = self.answers_by_no.get(no)
        now = (res.answer or "").strip()
        if prev and now and prev != now:
            msg = prompts.MSG_CONSIST_WARN.format(old=prev[:20], new=now[:20])
            self.log(msg)
            self.set_status(msg)
        self.answers_by_no[no] = now

    # ------------------------------------------------------------ 看门狗
    def _watch(self):
        try:
            if self.busy and self._task_t0:
                waited = time.time() - self._task_t0
                limit = float(self.cfg.get("timeout_s", 120) or 120)
                if waited > limit and not self._slow_warned:
                    self._slow_warned = True
                    self._slow_hint = True   # 下一题自动降级成单次采样
                    self.log(prompts.MSG_SLOW.format(sec=waited))
                    self.set_status(prompts.MSG_SLOW.format(sec=waited))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.after(2000, self._watch)
        except Exception:  # noqa: BLE001
            pass

    def open_history(self):
        """历史面板：最近做过的题（可导出 CSV 复习）。"""
        win = tk.Toplevel(self.root)
        win.title("做题历史")
        win.configure(bg=UI["bg"])
        apply_icon(win)
        win.geometry("760x460")
        try:
            from tkinter import ttk
        except Exception:  # noqa: BLE001
            return

        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure("SQA.Treeview", background=UI["card"], fieldbackground=UI["card"],
                        foreground=UI["text"], borderwidth=0, rowheight=26,
                        font=(FONT, 9))
        style.configure("SQA.Treeview.Heading", background=UI["card2"],
                        foreground=UI["dim"], font=(FONT, 9, "bold"))
        style.map("SQA.Treeview", background=[("selected", UI["accent"])])

        cols = ("ts", "answer", "fill", "confidence", "elapsed", "model")
        heads = {"ts": "时间", "answer": "答案", "fill": "填入",
                 "confidence": "置信度", "elapsed": "耗时", "model": "模型"}
        widths = {"ts": 140, "answer": 260, "fill": 120, "confidence": 70,
                  "elapsed": 60, "model": 120}
        frame = tk.Frame(win, bg=UI["bg"])
        frame.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        tree = ttk.Treeview(frame, columns=cols, show="headings",
                            style="SQA.Treeview")
        for c in cols:
            tree.heading(c, text=heads[c])
            tree.column(c, width=widths[c], anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        def reload():
            for iid in tree.get_children():
                tree.delete(iid)
            for r in cfg_mod.load_records(limit=500):
                tree.insert("", "end", values=(
                    r.get("ts", ""), (r.get("answer") or "")[:80],
                    (r.get("fill") or "")[:40], r.get("confidence", ""),
                    r.get("elapsed", ""), r.get("model", "")))
        reload()

        barw = tk.Frame(win, bg=UI["bg"])
        barw.pack(fill="x", padx=12, pady=10)
        stat = tk.Label(barw, text=f"共 {len(tree.get_children())} 条",
                        bg=UI["bg"], fg=UI["muted"], font=(FONT, 9))
        stat.pack(side="left")

        def do_export():
            name = f"screenqa-history-{time.strftime('%Y%m%d-%H%M%S')}.csv"
            target = ROOT / name
            n = cfg_mod.export_records_csv(target)
            if n:
                messagebox.showinfo("导出完成", f"已导出 {n} 条到：\n{target}")
                self.set_status(f"已导出 {n} 条历史 → {name}")
            else:
                messagebox.showwarning("导出失败", f"没有导出成功：\n{target}")

        def do_copy():
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            if vals and self._copy_text(str(vals[2] or vals[1])):
                self.set_status("已复制选中那条的答案")

        def do_export_pdf():
            recs = cfg_mod.load_records(limit=200)
            if not recs:
                messagebox.showwarning("导出失败", "还没有作答记录")
                return
            name = f"screenqa-复习-{time.strftime('%Y%m%d-%H%M%S')}.pdf"
            target = ROOT / name
            try:
                extras.export_pdf(recs, target)
                messagebox.showinfo("导出完成", f"已生成复习 PDF（{len(recs)} 题）：\n{target}")
                self.set_status(f"已导出复习资料 → {name}")
            except Exception as exc:  # noqa: BLE001
                messagebox.showwarning("导出失败", f"{type(exc).__name__}: {exc}")

        Pill(barw, "导出 PDF", do_export_pdf, kind="primary", height=30, padx=14,
             font_size=9, bg=UI["bg"]).pack(side="right")
        Pill(barw, "导出 CSV", do_export, kind="ghost", height=30, padx=14,
             font_size=9, bg=UI["bg"]).pack(side="right", padx=(0, 8))
        Pill(barw, "复制选中", do_copy, kind="ghost", height=30, padx=14,
             font_size=9, bg=UI["bg"]).pack(side="right", padx=(0, 8))
        Pill(barw, "刷新", reload, kind="ghost", height=30, padx=14,
             font_size=9, bg=UI["bg"]).pack(side="right", padx=(0, 8))

    def check_balance(self, silent: bool = False):
        """查 DeepSeek 余额 + 今日统计；silent=True 时只写日志不弹窗。"""
        def worker():
            try:
                from deepseek_api import DeepSeekClient
                client = DeepSeekClient(self.cfg.get("api_key", ""),
                                        self.cfg.get("base_url", ""))
                info = client.balance()
                stats = cfg_mod.daily_stats(self.cfg)
                avg = float(stats.get("avg_cost") or 0)
                amount = float(info.get("amount") or 0)
                cur = info.get("currency", "CNY")
                n_left = int(amount / avg) if avg > 0 else 0
                text = (f"余额 {amount:.2f} {cur}"
                        + (f"，约还能做 {n_left} 题" if n_left else "") +
                        f"\n今日 {stats['count']} 题 · 约 ¥{stats['cost']:.4f} · "
                        f"{stats['tokens_in'] + stats['tokens_out']} tokens")
                self.q.put(("balance", {"amount": amount, "cur": cur, "n": n_left}))
                self.note(f"💰 {text.split(chr(10))[0]}")
                if not silent:
                    self.root.after(0, lambda: messagebox.showinfo("余额与今日花费", text))
            except Exception as exc:  # noqa: BLE001
                msg = f"查余额失败：{exc}"
                self.q.put(("log", f"⚠ {msg}"))
                if not silent:
                    self.root.after(0, lambda: messagebox.showwarning("余额", msg))

        threading.Thread(target=worker, daemon=True).start()

    def check_update_async(self):
        """后台查更新（线程里只放队列，不碰 Tk —— root.after 在非主线程不安全）。"""
        def worker():
            try:
                info = extras.check_update()
            except Exception as exc:  # noqa: BLE001
                info = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.q.put(("update", info))
        threading.Thread(target=worker, daemon=True).start()

    def _on_update_info(self, info):
        self.update_info = info
        latest = str(info.get("latest") or "")
        seen = str(self.cfg.get("last_update_sha") or "")
        if info.get("ok") and latest and latest != seen:
            # 只在"第一次见到这个提交"时提醒，避免每次启动都刷
            self.cfg["last_update_sha"] = latest
            try:
                cfg_mod.save(self.cfg)
            except Exception:  # noqa: BLE001
                pass
            self.log(f"⬆ 远端有新提交 {latest[:7]}（{info.get('message', '')[:40]}）")
            self.set_status(f"远端有新提交 {latest[:7]} · ⚙ 里可下载")
        elif info.get("ok"):
            self.log(f"已是最新版本（{info.get('latest')}）")
        else:
            self.note(f"更新检查失败：{info.get('error', '')[:120]}")


    # ------------------------------------------------------------ 题面 / 翻译
    def toggle_excerpt(self, force=None):
        """展开/收起题面（可改字重算）与翻译。"""
        want = (not self.ex_visible) if force is None else bool(force)
        base = 759 if self.cfg.get("compact") else 794
        if want:
            self.ex.pack(fill="x", pady=(0, 8), before=self.det)
            self.ex_visible = True
            try:
                _sh = capture.screen_size()[1]
            except Exception:  # noqa: BLE001
                _sh = 1080
            place_bottom_right(self.root, 474, min(base + 150, _sh - 90))
            try:
                snap_bottom_right(self.root)
            except Exception:  # noqa: BLE001
                pass
        else:
            self.ex.pack_forget()
            self.ex_visible = False
            place_bottom_right(self.root, 474, base)
            try:
                snap_bottom_right(self.root)
            except Exception:  # noqa: BLE001
                pass

    def set_excerpt(self, pretty_q: str, translation: str = ""):
        try:
            self.ex_text.delete("1.0", "end")
            self.ex_text.insert("1.0", pretty_q or "")
            body = (translation or "").strip()
            if len(body) > 120:
                body = body[:120] + " …"
            body = body.replace("\n", " / ")
            self.tr_label.configure(
                text=(prompts.LBL_TRANSLATE + "：" + body) if body else "")
            self.tr_label.pack_forget()
            if translation:
                self.tr_label.pack(fill="x", padx=2, pady=(0, 8))
        except Exception:  # noqa: BLE001
            pass

    def recalc_from_excerpt(self):
        """把（改过的）题面文字直接送去作答，不再截图。"""
        try:
            text = self.ex_text.get("1.0", "end").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if len(text) < 8:
            self.set_status(prompts.MSG_TYPED_TOO_LITTLE if hasattr(prompts, "MSG_TYPED_TOO_LITTLE")
                            else "题面太短了，先框一次题目或自己补两句")
            return
        self.toggle_excerpt(True)
        self._start_text(text)


    # ------------------------------------------------------------ 账本
    def open_cloud_dialog(self):
        """云端：地址/令牌/登录注册/同步/测试。"""
        win = tk.Toplevel(self.root)
        win.title("云端（模型代理 + 记录同步）")
        win.configure(bg=UI["bg"])
        win.geometry("520x330")
        win.transient(self.root)
        bg, card = UI["bg"], UI["card"]
        tk.Label(win, text="把服务器地址填成 http://你的IP:端口；令牌登录后自动填。",
                 bg=bg, fg=UI["muted"], font=(FONT, 8), wraplength=480,
                 justify="left").pack(anchor="w", padx=18, pady=(14, 6))
        base_var = tk.StringVar(value=self.cfg.get("cloud_base", ""))
        tok_var = tk.StringVar(value=self.cfg.get("cloud_token", ""))

        def field(label, var, show=""):
            row = tk.Frame(win, bg=bg)
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(row, text=label, bg=bg, fg=UI["dim"], font=(FONT, 9),
                     width=8, anchor="w").pack(side="left")
            ent = tk.Entry(row, textvariable=var, show=show, bg=card, fg=UI["text"],
                           insertbackground=UI["text"], relief="flat", bd=5,
                           highlightthickness=1, highlightbackground=UI["border"],
                           highlightcolor=UI["accent"], font=(FONT, 10))
            ent.pack(side="left", fill="x", expand=True)
            return ent

        field("服务器", base_var)
        field("令牌", tok_var, show="\u2022")
        tip = tk.Label(win, text="", bg=bg, fg=UI["dim"], font=(FONT, 8), wraplength=480,
                       justify="left")
        tip.pack(anchor="w", padx=18, pady=(6, 0))

        def save_cloud(enabled):
            self.cfg["cloud_base"] = base_var.get().strip().rstrip("/")
            self.cfg["cloud_token"] = tok_var.get().strip()
            self.cfg["cloud_enabled"] = bool(enabled)
            cfg_mod.save(self.cfg)
            import deepseek_api as _dsa
            if enabled and self.cfg["cloud_base"] and self.cfg["cloud_token"]:
                _dsa.set_cloud(self.cfg["cloud_base"], self.cfg["cloud_token"])
                tip.configure(text="已开启云端代理：接下来的作答走你的服务器（本机 K不参与）。"
                                   .replace(" K不", "key 不"))
                self.note("云端代理已开启")
            else:
                _dsa.set_cloud("", "")
                tip.configure(text="已关闭云端代理：恢复用本机 Key 直连。")
                self.note("云端代理已关闭")

        def do_login(register=False):
            from tkinter import simpledialog
            name = simpledialog.askstring("云端账号", "用户名（2-20 字）", parent=win)
            if not name:
                return
            pw = simpledialog.askstring("云端账号", "密码（至少 6 位）", parent=win, show="\u2022")
            if not pw:
                return
            out = cloud_mod.login(base_var.get().strip(), name, pw, register=register)
            if out.get("token"):
                tok_var.set(out["token"])
                tip.configure(text=f"登录成功：{out.get('user', {}).get('name', name)}")
                save_cloud(True)
            else:
                tip.configure(text="失败：" + str(out.get("error")))

        def do_test():
            out = cloud_mod.health(base_var.get().strip())
            tip.configure(text="连通：" + str(out) if out.get("ok") else "连不上：" + str(out.get("error")))

        def do_sync():
            base, tok = base_var.get().strip(), tok_var.get().strip()
            if not (base and tok):
                tip.configure(text="先登录云端")
                return
            since = float(self.cfg.get("cloud_since") or 0)
            recs = cloud_mod.prepare_records(cfg_mod.PROFILE / "records.jsonl", since)
            res = cloud_mod.push(base, tok, recs, [])
            if res.get("error"):
                tip.configure(text="同步失败：" + str(res["error"]))
                return
            got = cloud_mod.pull(base, tok, since)
            import time as _t
            self.cfg["cloud_since"] = _t.time()
            cfg_mod.save(self.cfg)
            tip.configure(text=f"已上传 {res.get('saved', 0)} 条；云端有 "
                              f"{len(got.get('records') or [])} 条记录")

        rowb = tk.Frame(win, bg=bg)
        rowb.pack(fill="x", padx=18, pady=(10, 0))
        for text, fn, kind in (("登录", lambda: do_login(False), "ghost"),
                               ("注册", lambda: do_login(True), "ghost"),
                               ("测试连接", do_test, "ghost"),
                               ("同步记录", do_sync, "ghost"),
                               ("开启代理", lambda: save_cloud(True), "primary"),
                               ("关闭代理", lambda: save_cloud(False), "ghost")):
            Pill(rowb, text, fn, kind=kind, width=74, height=30).pack(side="left", padx=(0, 8))
        tk.Label(win, text="说明：开启后模型调用经过你的服务器，客户端不需要 DeepSeek Key；"
                          "余额查询在云端模式下不可用。",
                 bg=bg, fg=UI["muted"], font=(FONT, 8), wraplength=480,
                 justify="left").pack(anchor="w", padx=18, pady=(10, 12))

    def open_ledger(self):
        """今日/累计 + 按模型 + 30 天柱状图，可导出 CSV。"""
        try:
            recs = cfg_mod.load_records(800)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("账本", f"读记录失败：{exc}")
            return
        stat = ledger.summarize(recs)
        top = tk.Toplevel(self.root)
        top.title("账本 · 今日与近 30 天")
        top.configure(bg=UI["bg"])
        top.geometry("660x560")
        top.transient(self.root)
        head = tk.Label(top, bg=UI["bg"], fg=UI["text"], anchor="w", justify="left",
                        font=(FONT, 11, "bold"),
                        text=f"今日 {stat['today_n']} 题 · ¥{stat['today_cost']:.4f}"
                             f"    累计 {stat['total_n']} 题 · ¥{stat['total_cost']:.4f}")
        head.pack(fill="x", padx=16, pady=(14, 8))
        try:
            img = ledger.draw_chart(recs)
        except Exception:  # noqa: BLE001
            img = None
        if img is not None:
            self._chart_img = ImageTk.PhotoImage(img)
            tk.Label(top, image=self._chart_img, bg=UI["bg"], bd=0).pack(padx=12, pady=(0, 8))
        else:
            tk.Label(top, text="还没有作答记录（先做一道题）", bg=UI["bg"], fg=UI["dim"],
                     font=(FONT, 10)).pack(padx=16, pady=(0, 8))
        rows = list(stat["by_model"].items())
        if rows:
            box = tk.Frame(top, bg=UI["card2"], highlightbackground=UI["border"],
                           highlightthickness=1)
            box.pack(fill="x", padx=16)
            for name, m in rows[:6]:
                line = f"{name}：{m['n']} 次 · {m['tok']} tokens · ¥{m['cost']:.4f}"
                tk.Label(box, text=line, bg=UI["card2"], fg=UI["text"], anchor="w",
                         font=(FONT, 10)).pack(fill="x", padx=12, pady=3)
        btns = tk.Frame(top, bg=UI["bg"])
        btns.pack(fill="x", padx=16, pady=12)
        Pill(btns, "导出 CSV", lambda: self._export_ledger_csv(), kind="primary",
             height=30, font_size=9).pack(side="left")
        Pill(btns, "关闭", top.destroy, kind="ghost", height=30, font_size=9).pack(side="left", padx=(8, 0))

    def _export_ledger_csv(self):
        try:
            n = cfg_mod.export_records_csv()
            self.set_status(f"已导出 {n} 条记录到 records.csv")
            messagebox.showinfo("账本", f"已导出 {n} 条记录：\n{ROOT / 'records.csv'}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("账本", f"导出失败：{exc}")

    # ------------------------------------------------------------ 启动画面
    def show_splash(self):
        """1.1 秒的品牌启动画面（可在设置里关掉）。"""
        if not self.cfg.get("splash", True):
            return
        try:
            sp = tk.Toplevel(self.root)
            sp.overrideredirect(True)
            sp.configure(bg=UI["card"])
            sp.attributes("-topmost", True)
            w, h = 360, 140
            sp.geometry(f"{w}x{h}+{(self.root.winfo_screenwidth() - w) // 2}"
                        f"+{(self.root.winfo_screenheight() - h) // 2}")
            box = tk.Frame(sp, bg=UI["card"], highlightbackground=UI["border"],
                           highlightthickness=1)
            box.pack(fill="both", expand=True)
            tk.Label(box, text="屏幕问答", bg=UI["card"], fg=UI["text"],
                     font=(FONT, 18, "bold")).pack(pady=(28, 2))
            tk.Label(box, text="截屏 · 读题 · 作答 · 验算", bg=UI["card"], fg=UI["dim"],
                     font=(FONT, 10)).pack()
            tk.Label(box, text=f"v{prompts.VERSION_LABEL.lstrip('v')}", bg=UI["card"],
                     fg=UI["accent"], font=(FONT, 9)).pack(pady=(10, 0))
            sp.after(1100, sp.destroy)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 托盘
    def _setup_tray(self):
        self.tray_ok = False
        if not self.cfg.get("tray", True):
            return
        try:
            icon = ROOT / "assets" / "app.ico"
            if not icon.is_file():
                icon = ROOT / "assets" / "icon-64.png"
            self.tray = extras.TrayIcon(
                icon, f"{APP_TITLE}（左键显示/隐藏）",
                on_left=lambda: self.root.after(0, self.toggle_show),
                on_menu=lambda cid: self.root.after(0, lambda: self._on_tray_menu(cid)),
            )
            self.tray.start()
            if self.tray.ready.wait(3.0):
                if self.tray.error:
                    self.tray_ok = False
                    self.log(f"⚠ 托盘图标建不上：{self.tray.error} —— "
                             f"关窗口就是真退出（热键仍然可用）")
                    self.set_status("⚠ 托盘不可用 · 关窗口会退出程序")
                else:
                    self.tray_ok = True
                    self.log("托盘图标已就绪（左键显示/隐藏，右键菜单）")
        except Exception as exc:  # noqa: BLE001
            self.tray_ok = False
            self.log(f"⚠ 托盘初始化失败：{exc}")

    def toggle_show(self):
        """左右键都能用：隐藏 ↔ 显示（隐藏后热键照常工作）。"""
        try:
            if self.hidden:
                self.root.deiconify()
                self.root.attributes("-topmost", self.topmost)
                self.root.after(80, lambda: snap_bottom_right(self.root))
                self.hidden = False
                self.set_status("窗口已显示")
            else:
                self.root.withdraw()
                self.hidden = True
        except Exception:  # noqa: BLE001
            pass

    def _on_tray_menu(self, cid: int):
        if cid == 1:
            self.toggle_show()
        elif cid == 2:
            self.open_settings()
        elif cid == 3:
            self._quit()

    def _quit(self):
        self.cfg["tray"] = False
        try:
            cfg_mod.save(self.cfg)
        except Exception:  # noqa: BLE001
            pass
        self._on_close()

    def clear(self):
        self.text.delete("1.0", "end")
        self.ans.configure(text="按 Ctrl+Alt+Q 圈住题目", fg=UI["dim"])
        self.chip.configure(text="待命", bg=UI["card2"], fg=UI["muted"])
        self.edge.configure(bg=UI["accent"])
        try:
            self._quip_i = (getattr(self, "_quip_i", 0) + 1) % len(prompts.QUIPS)
            self.meta.configure(text=prompts.QUIP_PREFIX + prompts.QUIPS[self._quip_i])
        except Exception:  # noqa: BLE001
            self.meta.configure(text="")
        self.b_copy.set_enabled(False)
        self.b_redo.set_enabled(False)
        self.multi_items = []
        self.multi_idx = 0
        try:
            self.b_steps.set_enabled(False)
            self.b_bad.set_enabled(False)
        except Exception:  # noqa: BLE001
            pass
        self.set_status("就绪")

    # ------------------------------------------------------------ 设置
    def open_settings(self):
        bg, card = UI["bg"], UI["card"]
        card2 = UI["card2"]
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=bg)
        apply_icon(win)
        win.transient(self.root)
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        frm = tk.Frame(win, bg=bg)
        frm.pack(fill="both", expand=True, padx=18, pady=16)
        win.update_idletasks()
        w, h = 720, 820
        px = self.root.winfo_rootx() + self.root.winfo_width() - w
        py = self.root.winfo_rooty() + self.root.winfo_height() - h
        win.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

        vars_ = {}

        def field(row, label, key, value, show=None, width=40):
            tk.Label(frm, text=label, bg=bg, fg=UI["dim"], font=(FONT, 9),
                     anchor="w").grid(row=row, column=0, sticky="w", pady=6)
            var = tk.StringVar(value=value)
            ent = tk.Entry(frm, textvariable=var, width=width, show=show or "",
                           bg=card, fg=UI["text"], insertbackground=UI["text"],
                           relief="flat", bd=6, highlightthickness=1,
                           highlightbackground=UI["border"],
                           highlightcolor=UI["accent"], font=(FONT, 10))
            ent.grid(row=row, column=1, sticky="we", pady=6, padx=(10, 0))
            vars_[key] = var
            return ent

        key_entry = field(0, "DeepSeek API Key", "api_key",
                          self.cfg.get("api_key", ""), show="•", width=44)
        field(1, "接口地址", "base_url", self.cfg.get("base_url", "https://api.deepseek.com"))
        field(2, "视觉识别超时（秒）", "modlens_timeout",
              str(self.cfg.get("modlens_timeout", 300)), width=8)
        field(3, "连续截图间隔（秒）", "interval",
              str(self.cfg.get("interval", 3)), width=8)

        # 识别精度
        tk.Label(frm, text="识别精度", bg=bg, fg=UI["dim"], font=(FONT, 9),
                 anchor="w").grid(row=4, column=0, sticky="w", pady=6)
        qual_var = tk.StringVar(
            value="高精度（更准，并行）" if str(self.cfg.get("ocr_quality", "high")) == "high"
            else "标准（更快）")
        qual = tk.OptionMenu(frm, qual_var, "高精度（更准，并行）", "标准（更快）")
        qual.configure(bg=card, fg=UI["text"], activebackground=card2,
                       activeforeground=UI["text"], highlightthickness=1,
                       highlightbackground=UI["border"], bd=0, font=(FONT, 9),
                       indicatoron=True, width=22, anchor="w")
        qual["menu"].configure(bg=card, fg=UI["text"], activebackground=UI["accent"],
                               activeforeground="#ffffff", font=(FONT, 9))
        qual.grid(row=4, column=1, sticky="w", pady=6, padx=(10, 0))

        checks = tk.Frame(frm, bg=bg)
        checks.grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 8))
        top_var = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", True)))
        copy_var = tk.BooleanVar(value=bool(self.cfg.get("auto_copy", True)))
        esc_var = tk.BooleanVar(value=bool(self.cfg.get("escalate", True)))
        sound_var = tk.BooleanVar(value=bool(self.cfg.get("sound", True)))
        dedupe_var = tk.BooleanVar(value=bool(self.cfg.get("dedupe", True)))
        cache_var = tk.BooleanVar(value=bool(self.cfg.get("cache", True)))
        multi_var = tk.BooleanVar(value=bool(self.cfg.get("multi", True)))
        refocus_var = tk.BooleanVar(value=bool(self.cfg.get("refocus", True)))
        fast_var = tk.BooleanVar(value=bool(self.cfg.get("fast", False)))
        r1_var = tk.BooleanVar(value=bool(self.cfg.get("reasoner", True)))
        for i, (text, var) in enumerate((("窗口置顶", top_var),
                                         ("出答案自动复制", copy_var),
                                         ("分歧追加采样", esc_var),
                                         ("新题/出答案响铃", sound_var),
                                         ("同题不重复作答", dedupe_var),
                                         ("同题缓存秒出", cache_var),
                                         ("一屏多题分别作答", multi_var),
                                         ("答完回到原窗口", refocus_var),
                                         ("极速模式", fast_var),
                                         ("🟡时 R1 复核", r1_var))):
            tk.Checkbutton(checks, text=text, variable=var, bg=bg, fg=UI["dim"],
                           selectcolor=card, activebackground=bg,
                           activeforeground=UI["text"], bd=0, highlightthickness=0,
                           font=(FONT, 9)).grid(row=i // 3, column=i % 3,
                                                sticky="w", padx=(0, 16), pady=2)

        # ---- v2.4 新增开关（自动粘贴 / 双模型互检 / 几何图模式 / 托盘 / 自启 …）
        paste_var = tk.BooleanVar(value=bool(self.cfg.get("auto_paste", False)))
        enter_var = tk.BooleanVar(value=bool(self.cfg.get("auto_enter", False)))
        dual_var = tk.BooleanVar(value=bool(self.cfg.get("dual", False)))
        geom_var = tk.BooleanVar(value=bool(self.cfg.get("vision_first", False)))
        pos_var = tk.BooleanVar(value=bool(self.cfg.get("remember_pos", False)))
        tray_var = tk.BooleanVar(value=bool(self.cfg.get("tray", True)))
        boot_var = tk.BooleanVar(value=bool(self.cfg.get("autostart", False)))
        upd_var = tk.BooleanVar(value=bool(self.cfg.get("check_update", True)))
        watch_var = tk.BooleanVar(value=bool(self.cfg.get("watch_clip", False)))
        tts_var = tk.BooleanVar(value=bool(self.cfg.get("tts", False)))
        mini_var = tk.BooleanVar(value=bool(self.cfg.get("mini", False)))
        plot_var = tk.BooleanVar(value=bool(self.cfg.get("plot", True)))
        clipfb_var = tk.BooleanVar(value=bool(self.cfg.get("clip_fallback", True)))
        # v2.10：性能/读取策略开关
        adaptive_var = tk.BooleanVar(value=bool(self.cfg.get("adaptive", True)))
        stream_var = tk.BooleanVar(value=bool(self.cfg.get("stream", True)))
        fuzzy_var = tk.BooleanVar(value=bool(self.cfg.get("fuzzy_cache", True)))
        cross_var = tk.BooleanVar(value=bool(self.cfg.get("cross_check", True)))
        compact_var = tk.BooleanVar(value=bool(self.cfg.get("compact", False)))
        checks2 = tk.Frame(frm, bg=bg)
        checks2.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for i, (text, var) in enumerate((("答完自动粘贴", paste_var),
                                        ("再按回车提交", enter_var),
                                        ("双模型互检", dual_var),
                                        ("几何图模式", geom_var),
                                        ("记住窗口位置", pos_var),
                                        ("关窗缩到托盘", tray_var),
                                        ("开机自启", boot_var),
                                        ("启动查更新", upd_var),
                                        ("剪贴板自动作答", watch_var),
                                        ("答完自动朗读", tts_var),
                                        ("迷你答案条", mini_var),
                                        ("函数图像按钮", plot_var),
                                        (prompts.LBL_CLIP_FALLBACK, clipfb_var),
                                        ("自适应采样", adaptive_var),
                                        ("流式输出", stream_var),
                                        ("模糊缓存", fuzzy_var),
                                        ("双读校验", cross_var),
                                        ("紧凑模式", compact_var))):
            tk.Checkbutton(checks2, text=text, variable=var, bg=bg, fg=UI["dim"],
                           selectcolor=card, activebackground=bg,
                           activeforeground=UI["text"], bd=0, highlightthickness=0,
                           font=(FONT, 9)).grid(row=i // 4, column=i % 4,
                                                sticky="w", padx=(0, 12), pady=2)

        # ---- 外观：主题 / 字号 / 不透明度
        look = tk.Frame(frm, bg=bg)
        look.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(look, text="主题", bg=bg, fg=UI["dim"], font=(FONT, 9)).pack(side="left")
        theme_var = tk.StringVar(value="浅色" if self.cfg.get("theme") == "light" else "深色")
        theme_box = tk.OptionMenu(look, theme_var, "深色", "浅色")
        theme_box.configure(bg=card, fg=UI["text"], activebackground=card2,
                            activeforeground=UI["text"], highlightthickness=1,
                            highlightbackground=UI["border"], bd=0, font=(FONT, 9),
                            width=5, anchor="w")
        theme_box["menu"].configure(bg=card, fg=UI["text"],
                                    activebackground=UI["accent"],
                                    activeforeground="#ffffff", font=(FONT, 9))
        theme_box.pack(side="left", padx=(6, 14))
        tk.Label(look, text="字号 %", bg=bg, fg=UI["dim"], font=(FONT, 9)).pack(side="left")
        scale_var = tk.StringVar(value=str(self.cfg.get("font_scale", 100)))
        tk.Spinbox(look, from_=80, to=160, increment=10, width=5,
                   textvariable=scale_var, bg=card, fg=UI["text"],
                   insertbackground=UI["text"], relief="flat", bd=0,
                   buttonbackground=card2, highlightthickness=1,
                   highlightbackground=UI["border"], font=(FONT, 9)
                   ).pack(side="left", padx=(6, 14))
        tk.Label(look, text="考试日期", bg=bg, fg=UI["dim"],
                 font=(FONT, 9)).pack(side="left")
        exam_var = tk.StringVar(value=str(self.cfg.get("exam_date", "")))
        tk.Entry(look, textvariable=exam_var, width=11, bg=card, fg=UI["text"],
                 insertbackground=UI["text"], relief="flat", bd=0,
                 highlightthickness=1, highlightbackground=UI["border"],
                 font=(FONT, 9)).pack(side="left", padx=(6, 14))
        tk.Label(look, text="不透明度 %", bg=bg, fg=UI["dim"],
                 font=(FONT, 9)).pack(side="left")
        op_var = tk.StringVar(value=str(self.cfg.get("opacity", 100)))
        tk.Spinbox(look, from_=60, to=100, increment=5, width=5,
                   textvariable=op_var, bg=card, fg=UI["text"],
                   insertbackground=UI["text"], relief="flat", bd=0,
                   buttonbackground=card2, highlightthickness=1,
                   highlightbackground=UI["border"], font=(FONT, 9)
                   ).pack(side="left", padx=(6, 0))

        look2 = tk.Frame(frm, bg=bg)
        look2.grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(look2, text="语言", bg=bg, fg=UI["dim"],
                 font=(FONT, 9)).pack(side="left")
        lang_var = tk.StringVar(
            value=("English" if str(self.cfg.get("lang", "zh")).startswith("en") else "中文"))
        lang_box = tk.OptionMenu(look2, lang_var, "中文", "English")
        self._style_optionmenu(lang_box, width=7)
        lang_box.configure(bg=card, fg=UI["text"], activebackground=card2,
                           activeforeground=UI["text"], highlightthickness=0)
        lang_box.pack(side="left", padx=(6, 18))

        look3 = tk.Frame(frm, bg=bg)
        look3.grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 4))
        # 说明：这一行的 grid 行号与 look2 相同会被覆盖，故放在 look2 之前构建
        tk.Label(look3, text="\u4e3b\u9898\u8272", bg=bg, fg=UI["dim"], font=(FONT, 9)).pack(side="left")
        _acc_names = {"\u9752\u84dd": "blue", "\u6a31\u82b1": "sakura", "\u8584\u8377": "mint", "\u6d45\u91d1": "amber"}
        _cur = str(self.cfg.get("accent") or "blue")
        accent_pick = tk.StringVar(value=[k for k, v in _acc_names.items() if v == _cur][0])
        accent_box = tk.OptionMenu(look3, accent_pick, *_acc_names.keys())
        self._style_optionmenu(accent_box, width=6)
        accent_box.configure(bg=card, fg=UI["text"], activebackground=card2,
                            activeforeground=UI["text"], highlightthickness=0)
        accent_box.pack(side="left", padx=(6, 18))
        tk.Label(look3, text="\u670d\u52a1\u5546", bg=bg, fg=UI["dim"], font=(FONT, 9)).pack(side="left", padx=(16, 0))
        prov_pick = tk.StringVar(value=next((k for k, v in cfg_mod.PROVIDERS.items()
                                             if v[0] == str(self.cfg.get("base_url", ""))),
                                            "DeepSeek"))
        prov_box = tk.OptionMenu(look3, prov_pick, *cfg_mod.PROVIDERS.keys(),
                                 command=lambda _v: vars_["base_url"].set(
                                     cfg_mod.PROVIDERS[prov_pick.get()][0]))
        self._style_optionmenu(prov_box, width=11)
        prov_box.configure(bg=card, fg=UI["text"], activebackground=card2,
                           activeforeground=UI["text"], highlightthickness=0)
        prov_box.pack(side="left", padx=(6, 16))

        tk.Label(look3, text="\u7a97\u53e3", bg=bg, fg=UI["dim"], font=(FONT, 9)).pack(side="left")
        dock_pick = tk.StringVar(value="\u81ea\u7531\u7a97\u53e3" if str(self.cfg.get("dock")) == "free" else "\u8d34\u53f3\u4e0b\u89d2")
        dock_box = tk.OptionMenu(look3, dock_pick, "\u8d34\u53f3\u4e0b\u89d2", "\u81ea\u7531\u7a97\u53e3")
        self._style_optionmenu(dock_box, width=9)
        dock_box.configure(bg=card, fg=UI["text"], activebackground=card2,
                           activeforeground=UI["text"], highlightthickness=0)
        dock_box.pack(side="left", padx=(6, 0))

        accent_var = tk.BooleanVar(value=bool(self.cfg.get("follow_accent", True)))
        tk.Checkbutton(look2, text="跟随系统强调色", variable=accent_var, bg=bg, fg=UI["dim"],
                       selectcolor=card, activebackground=bg, activeforeground=UI["text"],
                       font=(FONT, 9), bd=0, highlightthickness=0
                       ).pack(side="left")
        tk.Label(look2, text="Mica 背景", bg=bg, fg=UI["dim"],
                 font=(FONT, 9)).pack(side="left", padx=(18, 0))
        mica_var = tk.BooleanVar(value=bool(self.cfg.get("mica", True)))
        tk.Checkbutton(look2, text="启用", variable=mica_var, bg=bg, fg=UI["dim"],
                       selectcolor=card, activebackground=bg, activeforeground=UI["text"],
                       font=(FONT, 9), bd=0, highlightthickness=0
                       ).pack(side="left", padx=(6, 0))

        row6 = tk.Frame(frm, bg=bg)
        row6.grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(row6, text=self._region_text(), bg=bg, fg=UI["muted"],
                 font=(FONT, 9)).pack(side="left")
        Pill(row6, "清空缓存", (lambda: msg.configure(
            text=f"已清空 {clear_cache()} 条缓存")),
             kind="flat", height=26, padx=12, font_size=9).pack(side="left", padx=(10, 0))

        def reset_region():
            self.cont_region = None
            self.cfg["cont_bbox"] = None
            cfg_mod.save(self.cfg)
            self.region_lbl.configure(text=self._region_text())
            msg.configure(text="区域已重置为整个屏幕")
            self.set_status("连续区域已重置")

        pill_reset = Pill(row6, "重置区域", reset_region, kind="flat", height=26,
                          padx=12, font_size=9, bg=bg)
        pill_reset.pack(side="left", padx=(10, 0))

        def vision_selftest():
            msg.configure(text="视觉自检中（可能要 1-3 分钟，失败会立刻报错）…")

            def worker():
                verdict = self._vision_selftest()
                def show():
                    msg.configure(text=verdict[:110])
                    self.detail.configure(state="normal")
                    self.detail.delete("1.0", "end")
                    self.detail.insert("end", verdict)
                    self.detail.configure(state="disabled")
                    if verdict.startswith("✅"):
                        messagebox.showinfo("视觉自检", verdict)
                    else:
                        messagebox.showwarning("视觉自检", verdict)
                self.root.after(0, show)

            threading.Thread(target=worker, daemon=True).start()

        def selfcheck():
            """一键体检：Key / OCR / 视觉 / 热键 / 连续区域，逐项给结论。"""
            lines = []
            try:
                key = (vars_.get("api_key").get() or "").strip() if vars_.get("api_key") else ""
                ok_key = key.startswith("sk-") and len(key) > 20
                lines.append(("✅" if ok_key else "❌") + " DeepSeek Key：" +
                             ("已填写" if ok_key else "没填 / 格式不对（应以 sk- 开头）"))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ Key 检查异常：{exc}")
            try:
                import psocr

                ok_ocr = psocr.available()
                lines.append(("✅" if ok_ocr else "❌") + " 本地 OCR（Windows）：" +
                             ("可用" if ok_ocr else "不可用（找不到 powershell.exe）"))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ 本地 OCR 检查异常：{exc}")
            try:
                import reader

                st = reader.backend_status()
                ok_v = bool(st.get("modlens", {}).get("available"))
                lines.append(("✅" if ok_v else "⚠️") + " 视觉模型（modlens）：" +
                             ("已找到，可用「视觉自检」实测" if ok_v
                              else "找不到 modlens / node —— 只影响几何图，文字题照常"))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"⚠️ 视觉模型检查异常：{exc}")
            try:
                bound = getattr(getattr(self, "hk", None), "bound", {}) or {}
                failed = getattr(getattr(self, "hk", None), "failed", []) or []
                if bound:
                    pairs = "、".join(f"{k}={v}" for k, v in bound.items())
                    lines.append("✅ 全局热键：" + pairs)
                if failed:
                    lines.append("⚠️ 没注册上的热键：" + "、".join(failed))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"⚠️ 热键检查异常：{exc}")
            try:
                if self.cont_region:
                    x, y, w, h = self.cont_region
                    lines.append(f"✅ 连续截图区域：({x},{y}) {w}×{h}")
                else:
                    lines.append("ℹ️ 连续截图区域：整个屏幕")
            except Exception:  # noqa: BLE001
                pass
            lines.append(f"ℹ️ 模型={self.cfg.get('model')} · 采样={self.cfg.get('samples')}"
                         f" · 精度={self.cfg.get('ocr_quality')} · 缓存={'开' if self.cfg.get('cache') else '关'}")
            text = "\n".join(lines)
            try:
                self.detail.configure(state="normal")
                self.detail.delete("1.0", "end")
                self.detail.insert("end", "【自检结果】\n" + text)
                self.detail.configure(state="disabled")
            except Exception:  # noqa: BLE001
                pass
            messagebox.showinfo("自检", text)

        Pill(row6, "自检", selfcheck, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))

        Pill(row6, "视觉自检", vision_selftest, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))

        def do_update():
            msg.configure(text="正在检查 GitHub 上的新版本…")

            def worker():
                info = extras.check_update()
                def show():
                    if not info.get("ok"):
                        msg.configure(text=f"检查失败：{info.get('error', '')[:80]}")
                        messagebox.showwarning("检查更新", f"没能连上 GitHub：\n"
                                                          f"{info.get('error', '')}")
                        return
                    latest = info.get("latest", "")
                    if info.get("behind"):
                        body = (f"本地版本 {APP_VERSION}\nGitHub 最新 {latest}\n\n"
                                f"最新提交：{info.get('message', '')}\n\n"
                                f"升级方式：git pull，或用 npx 重装技能；\n"
                                f"exe 用户重新下载发布包即可。\n{info.get('url', '')}")
                        msg.configure(text=f"有新版本 {latest}")
                        messagebox.showinfo("发现新版本", body)
                    else:
                        msg.configure(text=f"已是最新（{latest}）")
                        messagebox.showinfo("检查更新", f"已是最新版本（{latest}）")
                self.root.after(0, show)

            threading.Thread(target=worker, daemon=True).start()

        row7 = tk.Frame(frm, bg=bg)
        row7.grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))
        row8 = tk.Frame(frm, bg=bg)
        row8.grid(row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))
        Pill(row7, prompts.LBL_WARMUP, self.warmup, kind="flat", height=26,
             padx=12, font_size=9).pack(side="left", padx=(0, 6))
        Pill(row7, prompts.L("update"), do_update, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left")
        Pill(row7, prompts.L("history"), self.open_history, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))
        Pill(row7, "下载更新", self.download_update, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))
        Pill(row7, "安装更新", self.install_update, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))
        Pill(row7, "导出卡片", self.export_cards, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))
        Pill(row7, "余额", lambda: self.check_balance(silent=False), kind="flat",
             height=26, padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))
        Pill(row7, prompts.L("queue"), self.retry_queue, kind="flat",
             height=26, padx=12, font_size=9, bg=bg).pack(side="left", padx=(10, 0))
        Pill(row8, "云端", self.open_cloud_dialog, kind="flat", height=26,
             padx=12, font_size=9, bg=bg).pack(side="left", padx=(0, 6))
        Pill(row8, prompts.LBL_LEDGER, self.open_ledger, kind="flat",
             height=26, padx=12, font_size=9, bg=bg).pack(side="left", padx=(6, 0))

        msg = tk.Label(frm, text="Key 获取：platform.deepseek.com → API keys",
                       bg=bg, fg=UI["muted"], font=(FONT, 9))
        msg.grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 0))

        btns = tk.Frame(frm, bg=bg)
        btns.grid(row=13, column=0, columnspan=2, sticky="e", pady=(14, 0))

        def save():
            self.cfg["api_key"] = vars_["api_key"].get().strip()
            self.cfg["base_url"] = (vars_["base_url"].get().strip()
                                    or "https://api.deepseek.com")
            for key, lo, hi, dfl in (("modlens_timeout", 20, 900, 300),
                                     ("interval", 1, 60, 3)):
                try:
                    self.cfg[key] = max(lo, min(hi, int(float(vars_[key].get()))))
                except ValueError:
                    self.cfg[key] = dfl
            self.cfg["always_on_top"] = bool(top_var.get())
            self.cfg["auto_copy"] = bool(copy_var.get())
            self.cfg["escalate"] = bool(esc_var.get())
            self.cfg["sound"] = bool(sound_var.get())
            self.cfg["dedupe"] = bool(dedupe_var.get())
            self.cfg["cache"] = bool(cache_var.get())
            self.cfg["multi"] = bool(multi_var.get())
            self.cfg["refocus"] = bool(refocus_var.get())
            self.cfg["fast"] = bool(fast_var.get())
            self.cfg["reasoner"] = bool(r1_var.get())
            self.cfg["auto_paste"] = bool(paste_var.get())
            self.cfg["auto_enter"] = bool(enter_var.get())
            self.cfg["dual"] = bool(dual_var.get())
            self.cfg["vision_first"] = bool(geom_var.get())
            self.cfg["remember_pos"] = bool(pos_var.get())
            self.cfg["tray"] = bool(tray_var.get())
            self.cfg["check_update"] = bool(upd_var.get())
            self.cfg["theme"] = "light" if theme_var.get() == "浅色" else "dark"
            try:
                self.cfg["font_scale"] = max(80, min(160, int(float(scale_var.get()))))
            except Exception:  # noqa: BLE001
                self.cfg["font_scale"] = 100
            self.cfg["opacity"] = max(60, min(100, int(float(op_var.get() or 100))))
            # 开机自启：写注册表（失败也不拦着保存设置，只是提示）
            want_boot = bool(boot_var.get())
            if want_boot != bool(self.cfg.get("autostart", False)):
                ok = extras.set_autostart(want_boot)
                self.cfg["autostart"] = want_boot if ok else extras.autostart_enabled()
                if not ok:
                    messagebox.showwarning(
                        "开机自启", "写注册表被拒绝（可能被安全软件拦了）。\n"
                                   "可以手动把快捷方式放进「启动」文件夹。")
            # 托盘开关只在下次启动生效（托盘线程已经起来了）
            self.cfg["ocr_quality"] = (
                "high" if qual_var.get().startswith("高精度") else "standard")
            self.cfg["watch_clip"] = bool(watch_var.get())
            self.cfg["tts"] = bool(tts_var.get())
            self.cfg["mini"] = bool(mini_var.get())
            self.cfg["plot"] = bool(plot_var.get())
            self.cfg["clip_fallback"] = bool(clipfb_var.get())
            self.cfg["login"] = bool(login_var.get())
            self.cfg["adaptive"] = bool(adaptive_var.get())
            self.cfg["stream"] = bool(stream_var.get())
            self.cfg["fuzzy_cache"] = bool(fuzzy_var.get())
            self.cfg["cross_check"] = bool(cross_var.get())
            self.cfg["compact"] = bool(compact_var.get())
            self.cfg["exam_date"] = (exam_var.get() or "").strip()
            self.cfg["lang"] = "en" if lang_var.get().startswith("English") else "zh"
            self.cfg["follow_accent"] = bool(accent_var.get())
            self.cfg["accent"] = _acc_names.get(accent_pick.get(), "blue")
            _want_free = dock_pick.get().startswith("\u81ea\u7531")
            if _want_free != (getattr(self, "dock", "corner") == "free"):
                self.toggle_dock()
            self.cfg["mica"] = bool(mica_var.get())
            prompts.set_lang(self.cfg["lang"])
            cfg_mod.save(self.cfg)
            self.topmost = self.cfg["always_on_top"]
            try:
                self.root.attributes("-topmost", self.topmost)
            except Exception:  # noqa: BLE001
                pass
            self.lbl_pin.configure(fg=UI["accent"] if self.topmost else UI["muted"])
            self.interval_lbl.configure(text=f"每 {self.cfg['interval']} 秒")
            self.apply_opacity(self.cfg.get("opacity", 100), save=False)
            self.font_scale = max(80, min(160, int(self.cfg.get("font_scale", 100)))) / 100.0
            self.set_status("设置已保存" +
                            ("（主题/字号改完，重开窗口生效）"
                             if self.cfg.get("theme") or True else ""))
            win.destroy()

        Pill(btns, "保存", save, kind="primary", bold=True, width=88, bg=bg).pack(side="right")
        Pill(btns, "取消", win.destroy, kind="ghost", width=72, bg=bg).pack(
            side="right", padx=(0, 8))

        frm.columnconfigure(1, weight=1)
        key_entry.focus_set()
        try:
            win.grab_set()
        except Exception:  # noqa: BLE001
            pass


    # ------------------------------------------------ v2.6：纯文字 / 窗口 / 浮窗
    def solve_text(self):
        """纯文字直答：剪贴板里已经是题目文字时，跳过截图和 OCR。"""
        text = capture.clipboard_text().strip()
        if not text:
            self.set_status("剪贴板里没有文字 · 先复制题目文字（Ctrl+C）")
            self._beep("warn")
            return
        if len(text) < 8:
            self.set_status(f"剪贴板文字只有 {len(text)} 个字符，不像一道题")
            self._beep("warn")
            return
        if self.busy:
            return
        self.set_status("纯文字模式…")
        self._start_text(text)

    def _start_text(self, text: str):
        self._reset_stream()
        self.last_job = ("text", text)
        self._failed = False
        self.busy = True
        self._set_buttons(False)
        self._set_pulse(True)
        cfg = self._current_cfg()

        def emit(kind, payload):
            self.q.put((kind, payload))

        def work():
            try:
                payload = run_pipeline(None, cfg, emit, raw_text=text)
            except Exception:  # noqa: BLE001
                payload = {"ok": False, "stage": "crash",
                           "error": traceback.format_exc()[-900:]}
            self.q.put(("finish", payload))

        threading.Thread(target=work, daemon=True).start()

    def _start_clip_watch(self):
        try:
            self._clip_seq = capture.clipboard_sequence()
        except Exception:  # noqa: BLE001
            self._clip_seq = 0
        self.log("剪贴板监听已开：截图进剪贴板就自动作答（Ctrl+Alt+T 仍可纯文字作答）")
        self._clip_tick()

    def _clip_tick(self):
        try:
            if not self.root.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        try:
            seq = capture.clipboard_sequence()
            if seq and seq != self._clip_seq:
                self._clip_seq = seq
                if not self.busy and capture.clipboard_has_image():
                    img = capture.clipboard_image()
                    if img is not None and img.size[0] > 120 and img.size[1] > 60:
                        self._auto_from_clip(img)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.after(700, self._clip_tick)
        except Exception:  # noqa: BLE001
            pass

    def _auto_from_clip(self, img):
        # 只在指定窗口里自动作答（默认空 = 任何窗口），避免聊天截图误触发
        allow = [t.strip() for t in str(self.cfg.get("watch_titles") or "").split(",")
                 if t.strip()]
        if allow:
            title = capture.window_title(capture.foreground_window())
            if not any(t.lower() in title.lower() for t in allow):
                self.log(f"剪贴板有新图，但前台是「{title[:24]}」，跳过")
                return
        self.prev_hwnd = capture.foreground_window()
        path = self._shot_path()
        img.save(path)
        self.last_image = img
        self.last_image_path = str(path)
        self.set_status("剪贴板截图 → 作答中…")
        self._run_path(path)

    # -------------------------------------------------- 判卷 / 练习 / 卡片 / 重复区域
    def judge_answer(self):
        """把参考答案和"学生自己写的答案"一起交给模型批改。"""
        if not self.last_payload or not self.last_payload.get("ok"):
            self.set_status(prompts.MSG_NEED_ANSWER)
            self._beep("warn")
            return
        res = self.last_payload.get("result")
        std = (getattr(res, "fill", "") or getattr(res, "answer", "") or "").strip()
        try:
            mine = simpledialog.askstring(
                prompts.JUDGE_LABEL,
                "你填的是什么？（留空取消）\n参考答案：" + std,
                parent=self.root)
        except Exception:  # noqa: BLE001
            mine = None
        if not mine or not mine.strip():
            self.set_status(prompts.MSG_JUDGE_NOANSWER)
            return
        question = (self.last_payload.get("text") or "").strip()[:1500]
        self.set_status(prompts.MSG_JUDGE_RUN)
        cfg = self._current_cfg()

        def work():
            try:
                cli = DeepSeekClient(
                    cfg["api_key"], cfg["base_url"], cfg["model"],
                    int(cfg.get("timeout", 120)))
                msg = (f"题目：\n{question}\n\n参考答案：{std}\n\n"
                       f"学生答案：{mine.strip()}")
                out = cli.chat([{"role": "system", "content": prompts.JUDGE_SYSTEM},
                                {"role": "user", "content": msg}],
                               temperature=0.0, max_tokens=900)
                self.q.put(("judge", out))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("judge", f"❌ 批改失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_judge(self, text):
        self.judge_text = text
        self.text.delete("1.0", "end")
        self.text.insert("end", text, "answer" if "## 判定" in text else "")
        self.set_status("批改完成（Ctrl+C 可复制）")
        self.log("批改完成")

    def practice(self):
        payload = self.last_payload or {}
        text = (payload.get("text") or "").strip()
        if not text:
            self.set_status(prompts.MSG_PRACTICE_NONE)
            self._beep("warn")
            return
        cfg = self._current_cfg()
        self.set_status(prompts.MSG_PRACTICE_RUN)

        def work():
            try:
                cli = DeepSeekClient(
                    cfg["api_key"], cfg["base_url"], cfg["model"],
                    int(cfg.get("timeout", 120)))
                out = cli.chat([{"role": "system", "content": prompts.PRACTICE_SYSTEM},
                                {"role": "user", "content": text[:1500]}],
                               temperature=0.8, max_tokens=1200)
                self.q.put(("practice", out))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("practice", f"❌ 生成失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_practice(self, text):
        win = tk.Toplevel(self.root)
        win.title("同类型练习")
        win.configure(bg=UI["bg"])
        win.geometry("560x520")
        apply_icon(win)
        tk.Label(win, text="同类型练习（3 题，含答案）", bg=UI["bg"], fg=UI["text"],
                 font=(FONT, 11, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        box = tk.Text(win, wrap="word", bg=UI["card2"], fg=UI["text"], bd=0,
                      relief="flat", padx=14, pady=12, font=(FONT, 10))
        box.pack(fill="both", expand=True, padx=14)
        box.insert("end", text)
        box.configure(state="disabled")
        row = tk.Frame(win, bg=UI["bg"])
        row.pack(fill="x", padx=14, pady=10)
        Pill(row, "复制", lambda: self._copy_text(text), kind="primary",
             width=72).pack(side="left")
        Pill(row, "关闭", win.destroy, kind="ghost", width=72).pack(side="right")
        save = ROOT / "练习"
        try:
            save.mkdir(exist_ok=True)
            (save / (time.strftime("%Y%m%d-%H%M%S") + "-练习.md")).write_text(
                text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self.set_status("练习已生成（也存到了 练习/ 目录）")

    def export_card(self):
        """把当前这题导成一张 PNG 卡片。"""
        payload = self.last_payload or {}
        res = payload.get("result")
        if not res:
            self.set_status(prompts.MSG_NEED_ANSWER)
            return
        rec = {"title": (payload.get("text") or "")[:200],
               "answer": getattr(res, "answer", ""),
               "fill": getattr(res, "fill", ""),
               "confidence": getattr(res, "confidence", ""),
               "elapsed": payload.get("elapsed", 0),
               "model": self.cfg.get("model", ""),
               "ts": time.strftime("%Y-%m-%d %H:%M")}
        out_dir = ROOT / "cards"
        out = out_dir / (time.strftime("%Y%m%d-%H%M%S") + "-卡片.png")
        try:
            extras.export_card(rec, getattr(self, "shot_path", ""), out)
            self.set_status(prompts.MSG_CARD_OK.format(path=str(out)))
            self.note(f"卡片已保存 {out}")
        except Exception as exc:  # noqa: BLE001
            self.set_status(prompts.MSG_CARD_FAIL.format(err=str(exc)[:80]))

    def repeat_region(self):
        """用上次框选的区域再截一次（考试时最快）。"""
        if not self.last_region:
            self.set_status(prompts.MSG_REPEAT_NONE)
            self._beep("warn")
            return
        if self.busy:
            return
        try:
            self.prev_hwnd = capture.foreground_window()
            img = capture.grab_bbox(*self.last_region)
        except Exception as exc:  # noqa: BLE001
            self.log(f"重复区域截图失败：{exc}")
            return
        self._start(img)

    def capture_window_menu(self):
        wins = capture.list_windows()
        if not wins:
            self.set_status("没找到可截图的窗口")
            return
        menu = tk.Menu(self.root, tearoff=0, bg=UI["card"], fg=UI["text"],
                       activebackground=UI["accent"], activeforeground="#ffffff",
                       bd=0)
        for hwnd, title in wins[:18]:
            menu.add_command(label=title[:46],
                             command=lambda h=hwnd, t=title: self._shot_window(h, t))
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            try:
                menu.grab_release()
            except Exception:  # noqa: BLE001
                pass

    def _shot_window(self, hwnd: int, title: str):
        if self.busy:
            return
        img = capture.grab_window(hwnd)
        if img is None:
            self.set_status(f"抓不到「{title[:18]}」的画面（可能已最小化）")
            self._beep("warn")
            return
        self.prev_hwnd = capture.foreground_window()
        self.last_image = img
        path = self._shot_path()
        img.save(path)
        self.last_image_path = str(path)
        self.log(f"抓取窗口「{title[:28]}」→ {img.size[0]}×{img.size[1]}")
        self._run_path(path)

    def toggle_mini(self):
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
            self.log("迷你答案条已关")
            return
        self.mini = MiniBar(self.root, UI, self.copy_answer, self.toggle_mini)
        if self.last_payload:
            res = self.last_payload.get("result")
            if res is not None:
                label, key = CONF_CHIP.get(res.confidence, ("", "accent"))
                self.mini.set_text(f"{res.fill or res.answer}", key)
        self.log("迷你答案条已开（拖动可移动，双击复制，✕ 关闭）")

    def speak_answer(self):
        if not self.last_payload:
            self.set_status("还没有答案可读")
            return
        res = self.last_payload.get("result")
        text = (res.fill or res.answer) if res is not None else ""
        if not text:
            return
        ok = extras.speak(f"答案是 {text}", self.cfg.get("tts_lang", "zh"))
        self.set_status("🔊 正在朗读…" if ok else "朗读失败（系统语音不可用）")

    def show_plot(self):
        if not self.last_payload:
            return
        text = self.last_payload.get("text") or ""
        exprs = plotter.extract_functions(text)
        if not exprs:
            expr1 = plotter.extract_function(text)
            exprs = [expr1] if expr1 else []
        if not exprs:
            self.set_status("题目里没找到 f(x)=… / y=… 形式的函数")
            return
        img = plotter.plot_multi(exprs) if len(exprs) > 1 else plotter.plot_function(exprs[0])
        if img is None:
            self.set_status("这个函数画不出来（表达式可能带 OCR 噪声）")
            return
        self.plot_img = img
        d = ROOT / "_tmp"
        try:
            d.mkdir(exist_ok=True)
            path = d / "plot.png"
            img.save(path)
        except Exception:  # noqa: BLE001
            path = Path(tempfile.gettempdir()) / "screenqa_plot.png"
            img.save(path)
        top = tk.Toplevel(self.root)
        top.title("函数图像")
        top.configure(bg=UI["bg"])
        apply_icon(top)
        try:
            top.transient(self.root)
        except Exception:  # noqa: BLE001
            pass
        photo = tk.PhotoImage(file=str(path))
        lbl = tk.Label(top, image=photo, bg=UI["bg"])
        lbl.image = photo
        lbl.pack(padx=12, pady=12)
        tk.Label(top, text=f"y = {expr}  ·  零点已标出", bg=UI["bg"],
                 fg=UI["dim"], font=("Microsoft YaHei UI", 9)).pack(pady=(0, 10))

    def _on_close(self):
        # 关窗口 = 缩到托盘（考试中手滑点 X 不会把程序关掉）；托盘菜单「退出」才真退出
        if (self.cfg.get("tray", True) and self.tray is not None
                and getattr(self, "tray_ok", False)):
            try:
                self.root.withdraw()
                self.hidden = True
                self.set_status("已缩到托盘 · 左键托盘图标可召回")
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            if getattr(self, "dock", "corner") == "free":
                self.cfg["win_size"] = self.root.geometry()
            if self.cfg.get("remember_pos"):
                self.cfg["last_pos"] = self.root.geometry()
            cfg_mod.save(self.cfg)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.stop_evt.set()
        except Exception:  # noqa: BLE001
            pass
        if self.hk:
            self.hk.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


def main():
    try:  # 必须在 tk.Tk() 之前：否则 Tk 用虚拟化坐标，窗口贴不到右下角
        import capture as _cap
        _cap.enable_dpi_awareness()
    except Exception:  # noqa: BLE001
        pass
    ensure_bundled_files()
    ap = argparse.ArgumentParser(description="屏幕问答助手")
    ap.add_argument("--cli", action="store_true", help="无界面模式，截全屏并作答")
    ap.add_argument("--image", help="直接处理指定图片")
    ap.add_argument("--save", action="store_true", help="保存到 history/")
    ap.add_argument("--auto-cont", action="store_true",
                    help="启动后自动进入连续截图模式（调试用）")
    args = ap.parse_args()

    if args.cli or args.image:
        sys.exit(cli_main(args))

    root = tk.Tk()
    root.withdraw()
    _c0 = cfg_mod.load()
    if _c0.get("cloud_enabled") and _c0.get("cloud_base") and _c0.get("cloud_token"):
        import deepseek_api as _dsa
        _dsa.set_cloud(_c0["cloud_base"], _c0["cloud_token"])
    # 本机账号（可游客进入）：不同用户各自的 records/errors/缓存/配置
    try:
        if cfg_mod.load().get("login", True):
            import accounts
            who = accounts.ask_login(root)
            if who is None:
                root.destroy()
                sys.exit(0)
            if who:
                cfg_mod.set_profile(accounts.safe_name(who))
                global CACHE_PATH
                CACHE_PATH = cfg_mod.PROFILE / "cache.json"
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print("登录窗异常（直接以游客进入）：", exc)
    app = App(root)
    root.deiconify()
    try:
        app.show_splash()
    except Exception:  # noqa: BLE001
        pass
    if args.auto_cont:
        root.after(900, app.start_continuous)
    root.mainloop()


def _fatal_popup(text: str) -> None:
    """启动/运行中崩了：既要写日志，也要弹窗——绝不"点了没反应"。"""
    try:
        with open(ROOT / "_startup_error.log", "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("屏幕问答 · 启动失败", text[-1500:])
        r.destroy()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        import traceback

        _fatal_popup("启动失败，请把这段发给开发者：\n\n" + traceback.format_exc()[-1500:])
        raise
