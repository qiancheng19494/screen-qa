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
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capture  # noqa: E402
import config as cfg_mod  # noqa: E402
import questions as qsplit  # noqa: E402
import reader  # noqa: E402
from deepseek_api import DeepSeekClient  # noqa: E402
from solver import SolveResult, Solver  # noqa: E402

APP_TITLE = "屏幕问答 · DeepSeek"

READER_LABELS = {
    "自动（视觉优先）": ["modlens", "winocr"],
    "仅视觉模型": ["modlens"],
    "仅本地 OCR": ["winocr"],
}
MODELS = ["deepseek-chat", "deepseek-reasoner"]
SAMPLE_CHOICES = ["1", "2", "3", "5"]

UI = {
    "bg": "#0a0c10",
    "card": "#131820",
    "card2": "#0d1117",
    "border": "#232b38",
    "text": "#e9ecf2",
    "dim": "#98a2b3",
    "muted": "#6a7486",
    "accent": "#4d9dff",
    "green": "#3ddc84",
    "green_bg": "#0d2a1c",
    "amber": "#ffc857",
    "amber_bg": "#2b2310",
    "red": "#ff6b6b",
    "red_bg": "#2b1416",
}
FONT = "Microsoft YaHei UI"
CONF_CHIP = {
    "高": ("🟢 高置信度", "green"),
    "中": ("🟡 中置信度", "amber"),
    "存疑": ("🔴 存疑 · 请核对", "red"),
}


# ---------------------------------------------------------------- 小工具
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


def dark_titlebar(root: tk.Tk) -> None:
    """Windows 10/11 深色标题栏。"""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        one = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(one), ctypes.sizeof(one))
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


def run_pipeline(image_path: str, cfg: dict, emit) -> dict:
    t0 = time.time()

    reader_order = cfg.get("reader_order") or ["modlens", "winocr"]
    emit("log", f"识别中（{' → '.join(reader_order)}）…")
    rr = reader.read_image(
        image_path,
        order=reader_order,
        modlens_timeout=int(cfg.get("modlens_timeout", 300)),
        log=lambda m: emit("log", m),
        ocr_quality=str(cfg.get("ocr_quality", "high")),
    )
    if not rr.text:
        return {"ok": False, "stage": "read", "error": rr.error,
                "elapsed": time.time() - t0}
    if len(rr.text.strip()) < 12:
        return {"ok": False, "stage": "read", "elapsed": time.time() - t0,
                "error": (f"只识别到 {len(rr.text.strip())} 个字，基本等于没框到题目。"
                          f"\n把题目（含题干和选项）框大一点，或按 {self._key_name('full', 'ctrl+alt+w')} 整屏。")}
    emit("log", f"识别完成（{rr.backend}，{len(rr.text)} 字，{rr.elapsed:.1f}s）")

    key = _cache_key(rr.text, cfg)

    # ---- 缓存命中：同一道题做过就直接秒出，不再花钱调模型
    if cfg.get("cache", True):
        hit = load_cache().get(key)
        if hit and hit.get("items"):
            items = [_dict_to_res(d) for d in hit["items"]]
            _fast_downgrade(items, cfg)
            emit("log", f"⚡ 命中缓存（省下 {sum(i.calls for i in items)} 次模型调用）")
            return {
                "ok": True, "text": rr.text, "backend": rr.backend,
                "read_elapsed": rr.elapsed, "result": items[0], "items": items,
                "elapsed": time.time() - t0, "warnings": rr.warnings,
                "cached": True, "cached_at": hit.get("ts", 0),
            }

    client = DeepSeekClient(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
    )
    solver = Solver(
        client,
        samples=int(cfg.get("samples", 3)),
        verify=bool(cfg.get("verify", True)),
        temperature=float(cfg.get("temperature", 0.7)),
        progress=lambda m: emit("log", m),
        escalate=bool(cfg.get("escalate", True)),
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
    }

    # ---- 成功作答才写缓存（失败的不缓存，免得下次直接给出错误结论）
    if (cfg.get("cache", True) and items
            and not items[0].answer.startswith("（调用失败）")):
        try:
            data = load_cache()
            data[key] = {"ts": time.time(), "text": rr.text[:4000],
                         "items": [_res_to_dict(r) for r in items]}
            save_cache(data)
        except Exception:  # noqa: BLE001
            pass

    return payload


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
    if payload.get("cached"):
        lines.append(f"⚡ 命中缓存：直接秒出（当初算了 {res.elapsed:.1f}s / "
                     f"{res.calls} 次调用，本次只花 {payload['elapsed']:.1f}s）")
    else:
        lines.append(f"{icon} 置信度：{res.confidence}（样本一致度 "
                     f"{res.agreement:.0%}，{res.calls} 次模型调用，"
                     f"{res.elapsed:.1f}s）")
    lines += ["", "—— 详细推理 ——", (res.reasoning or "（无）").strip()]
    if len(res.samples) > 1:
        lines += ["", "—— 各次采样 ——"]
        lines += [f"{i}. {x}" for i, x in enumerate(res.samples, 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------- 圆角按钮
class Pill(tk.Canvas):
    """Canvas 手绘的圆角按钮，带 hover / 按下 / 禁用。"""

    KINDS = {
        "primary": ("#4d9dff", "#07121f", "#71b4ff"),
        "ghost": ("#1a212c", "#dde3ec", "#242d3b"),
        "success": ("#10301e", "#54eda0", "#17452c"),
        "danger": ("#331a1d", "#ff9b9b", "#48262b"),
        "flat": ("#0d1117", "#98a2b3", "#1a212c"),
    }

    def __init__(self, master, text, command=None, kind="ghost", height=34,
                 padx=16, radius=11, font_size=10, bold=False, width=None,
                 bg=None):
        self._c_bg = bg or master["bg"]
        fg, fill, hover = self.KINDS.get(kind, self.KINDS["ghost"])
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
        fg, fill, hover = self.KINDS.get(kind, self.KINDS["ghost"])
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
def cli_main(args) -> int:
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
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = cfg_mod.load()
        self.q: queue.Queue = queue.Queue()
        self.busy = False
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

        capture.enable_dpi_awareness()
        self._init_window()
        self._build()
        self._setup_hotkeys()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(60, lambda: dark_titlebar(self.root))
        self.root.after(160, lambda: snap_bottom_right(self.root))
        self.root.after(700, lambda: snap_bottom_right(self.root))
        # 被最小化 / Win+D 之后再恢复时，重新贴回右下角
        self.root.bind("<Map>", self._on_map)

        if not self.cfg.get("api_key"):
            self.set_status("⚠ 还没填 API Key —— 点右上角 ⚙")
        else:
            self._apply_hotkey_labels()

    # ------------------------------------------------------------ 窗口
    def _init_window(self):
        root = self.root
        root.title(APP_TITLE)
        root.configure(bg=UI["bg"])
        place_bottom_right(root, 474, 668)
        root.minsize(430, 540)
        self.topmost = bool(self.cfg.get("always_on_top", True))
        root.attributes("-topmost", self.topmost)

    def _on_map(self, _ev=None):
        """窗口从最小化恢复（或 Win+D 后恢复）时，再贴一次右下角。"""
        try:
            if self.root.state() == "normal":
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
        head = tk.Frame(wrap, bg=bg)
        head.pack(fill="x")
        self.dot = tk.Label(head, text="●", bg=bg, fg=UI["accent"], font=(FONT, 11))
        self.dot.pack(side="left")
        tk.Label(head, text=" 屏幕问答", bg=bg, fg=UI["text"],
                 font=(FONT, 13, "bold")).pack(side="left")
        tk.Label(head, text="DeepSeek", bg=bg, fg=UI["muted"],
                 font=(FONT, 9)).pack(side="left", padx=(8, 0))

        self.lbl_gear = tk.Label(head, text="⚙", bg=bg, fg=UI["dim"],
                                 font=(FONT, 13), cursor="hand2")
        self.lbl_gear.pack(side="right")
        self.lbl_gear.bind("<Button-1>", lambda e: self.open_settings())
        self.lbl_pin = tk.Label(head, text="📌", bg=bg,
                                fg=UI["accent"] if self.topmost else UI["muted"],
                                font=(FONT, 11), cursor="hand2")
        self.lbl_pin.pack(side="right", padx=(0, 10))
        self.lbl_pin.bind("<Button-1>", lambda e: self.toggle_topmost())

        # ---- 答案卡
        card = tk.Frame(wrap, bg=UI["card"], highlightbackground=UI["border"],
                        highlightthickness=1)
        card.pack(fill="x", pady=(11, 9))
        inner = tk.Frame(card, bg=UI["card"])
        inner.pack(fill="both", expand=True, padx=15, pady=13)
        self.ans = tk.Label(inner, text="按 Ctrl+Alt+Q 圈住题目", bg=UI["card"],
                            fg=UI["dim"], font=(FONT, 15, "bold"), justify="left",
                            anchor="w", wraplength=390)
        self.ans.pack(fill="x")
        chips = tk.Frame(inner, bg=UI["card"])
        chips.pack(fill="x", pady=(9, 0))
        self.chip = tk.Label(chips, text="待命", bg=UI["card2"], fg=UI["muted"],
                             font=(FONT, 9), padx=9, pady=2)
        self.chip.pack(side="left")
        self.meta = tk.Label(chips, text="", bg=UI["card"], fg=UI["muted"],
                             font=(FONT, 9), anchor="w")
        self.meta.pack(side="left", padx=(9, 0), fill="x")
        self.card = card
        card.bind("<Configure>", self._on_card_resize)

        # ---- 选项行
        opts = tk.Frame(wrap, bg=bg)
        opts.pack(fill="x", pady=(0, 8))
        tk.Label(opts, text="识别", bg=bg, fg=UI["muted"], font=(FONT, 9)).pack(side="left")
        self.reader_var = tk.StringVar(value=self._reader_label())
        self.reader_box = tk.OptionMenu(opts, self.reader_var, *READER_LABELS,
                                        command=lambda *_: self._on_reader_change())
        self._style_optionmenu(self.reader_box, width=13)
        self.reader_box.pack(side="left", padx=(6, 10))

        tk.Label(opts, text="模型", bg=bg, fg=UI["muted"], font=(FONT, 9)).pack(side="left")
        self.model_var = tk.StringVar(value=self.cfg.get("model", "deepseek-chat"))
        self.model_box = tk.OptionMenu(opts, self.model_var, *MODELS)
        self._style_optionmenu(self.model_box, width=15)
        self.model_box.pack(side="left", padx=(6, 10))

        tk.Label(opts, text="采样", bg=bg, fg=UI["muted"], font=(FONT, 9)).pack(side="left")
        self.samples_var = tk.StringVar(value=str(self.cfg.get("samples", 3)))
        self.samples_box = tk.OptionMenu(opts, self.samples_var, *SAMPLE_CHOICES)
        self._style_optionmenu(self.samples_box, width=3)
        self.samples_box.pack(side="left", padx=(6, 0))

        self.verify_var = tk.BooleanVar(value=bool(self.cfg.get("verify", True)))
        tk.Checkbutton(opts, text="审核", variable=self.verify_var, bg=bg,
                       fg=UI["dim"], selectcolor=UI["card2"], activebackground=bg,
                       activeforeground=UI["text"], bd=0, highlightthickness=0,
                       font=(FONT, 9)).pack(side="right")

        # ---- 详情
        det = tk.Frame(wrap, bg=UI["card2"], highlightbackground=UI["border"],
                       highlightthickness=1)
        det.pack(fill="both", expand=True)
        self.text = tk.Text(det, wrap="word", bg=UI["card2"], fg=UI["text"],
                            insertbackground=UI["text"], relief="flat", bd=0,
                            highlightthickness=0, cursor="arrow", height=8,
                            font=(FONT, int(self.cfg.get("font_size", 11))),
                            padx=14, pady=12, spacing1=2, spacing3=3)
        sb = tk.Scrollbar(det, command=self.text.yview, width=9, bg=UI["card2"],
                          troughcolor=UI["card2"], activebackground=UI["border"],
                          relief="flat", bd=0, highlightthickness=0)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("answer", font=(FONT, 14, "bold"),
                                foreground=UI["green"], spacing1=3, spacing3=6)
        self.text.tag_configure("muted", foreground=UI["dim"])
        self.text.tag_configure("warn", foreground=UI["amber"])
        self.text.tag_configure("err", foreground=UI["red"])
        self.text.tag_configure("h", foreground=UI["accent"],
                                font=(FONT, 10, "bold"), spacing1=8, spacing3=3)
        self.text.bind("<Key>", self._block_keys)

        # ---- 操作
        bar = tk.Frame(wrap, bg=bg)
        bar.pack(fill="x", pady=(10, 0), before=det)
        self.b_region = Pill(bar, "框选截图", self.shot_region, kind="primary",
                             bold=True, width=104)
        self.b_region.pack(side="left")
        self.b_full = Pill(bar, "全屏", self.shot_full, kind="ghost", width=64)
        self.b_full.pack(side="left", padx=(7, 0))
        self.b_paste = Pill(bar, "粘贴", self.paste_image, kind="ghost", width=64)
        self.b_paste.pack(side="left", padx=(7, 0))
        self.b_redo = Pill(bar, "重算", self.redo, kind="ghost", width=60)
        self.b_redo.pack(side="left", padx=(7, 0))
        self.b_redo.set_enabled(False)
        self.b_copy = Pill(bar, "复制答案", self.copy_answer, kind="ghost", width=84)
        self.b_copy.pack(side="right")
        self.b_copy.set_enabled(False)

        # 一屏多题时，这里会冒出「复制第 N 题」按钮；空的时候不占高度
        self.multi_bar = tk.Frame(wrap, bg=bg)
        self.multi_bar.pack(fill="x", before=bar)

        bar2 = tk.Frame(wrap, bg=bg)
        bar2.pack(fill="x", pady=(8, 0), before=det)
        self.b_cont = Pill(bar2, "▶  连续截图", self.toggle_continuous,
                           kind="success", bold=True, width=118)
        self.b_cont.pack(side="left")
        self.region_lbl = tk.Label(bar2, text=self._region_text(), bg=bg,
                                   fg=UI["muted"], font=(FONT, 9), cursor="hand2")
        self.region_lbl.pack(side="left", padx=(9, 0))
        self.region_lbl.bind("<Button-1>", lambda e: self.pick_region(quiet=True))
        self.interval_lbl = tk.Label(bar2, text=f"每 {self.cfg.get('interval', 3)} 秒",
                                     bg=bg, fg=UI["muted"], font=(FONT, 9),
                                     cursor="hand2")
        self.interval_lbl.pack(side="right")
        self.interval_lbl.bind("<Button-1>", lambda e: self.open_settings())

        # ---- 状态栏
        st = tk.Frame(self.root, bg=UI["card2"])
        st.pack(fill="x", side="bottom", before=wrap)
        self.status = tk.Label(st, text="就绪", bg=UI["card2"], fg=UI["muted"],
                               font=(FONT, 9), anchor="w")
        self.status.pack(side="left", padx=12, pady=4)

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

    def _block_keys(self, ev):
        if ev.state & 0x4 and ev.keysym.lower() in ("c", "a", "insert"):
            return None
        if ev.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next",
                         "Home", "End", "Shift_L", "Shift_R", "Control_L",
                         "Control_R", "Tab"):
            return None
        return "break"

    def _reader_label(self):
        order = self.cfg.get("reader_order") or ["modlens", "winocr"]
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

    def set_status(self, msg: str):
        self.status.configure(text=msg)

    def _flash(self, color: str):
        try:
            self.card.configure(highlightbackground=color)
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
        if self.busy:
            return
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
                                                ["modlens", "winocr"])
        cfg["model"] = self.model_var.get()
        cfg["samples"] = int(self.samples_var.get())
        cfg["verify"] = bool(self.verify_var.get())
        if cfg.get("fast"):
            cfg["samples"] = 1        # 极速：只问一次
            cfg["verify"] = False     # 不做交叉审核
            cfg["escalate"] = False   # 不追加采样
        return cfg

    def _set_buttons(self, enabled: bool):
        for b in (self.b_region, self.b_full, self.b_paste):
            b.set_enabled(enabled)

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
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "status":
                    self.set_status(str(payload))
                elif kind == "busy":
                    self.busy = bool(payload)
                    self._set_buttons(not self.busy)
                elif kind == "finish":
                    self._finish(payload)
        except queue.Empty:
            pass

        if self.hk is not None:
            try:
                while True:
                    name = self.hk.events.get_nowait()
                    if name == "region":
                        self.shot_region()
                    elif name == "full":
                        self.shot_full()
                    elif name == "redo":
                        self.redo()
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
            tips = [f"就绪 · 按 {region} 圈题"]
            full = self._key_name("full", "ctrl+alt+w")
            want_full = (self.cfg.get("hotkey_full") or "ctrl+alt+w").lower()
            if (getattr(self.hk, "bound", None) or {}).get("full", want_full) != want_full:
                tips.append(f"整屏 {full}")
                self.log(f"ℹ Ctrl + Alt + W 被别的软件占用，整屏已改用 {full}")
            redo = self._key_name("redo", "ctrl+alt+r")
            tips.append(f"重算 {redo}")
            self.set_status(" · ".join(tips))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 结果渲染
    def _finish(self, payload):
        self.busy = False
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
            self.meta.configure(text=str(payload.get("stage", "")))
            self._flash(UI["red"])
            self.text.delete("1.0", "end")
            self.text.insert("end", "❌ 失败：\n", "err")
            self.text.insert("end", str(payload.get("error")) + "\n")
            self.set_status("失败 · 见详情")
            self._beep("warn")
            return

        res = payload["result"]
        if res.answer.startswith("（调用失败）"):
            self.ans.configure(text="DeepSeek 调用失败", fg=UI["red"])
            self.chip.configure(text="🔴 未作答", bg=UI["red_bg"], fg=UI["red"])
            self.meta.configure(text=str(res.error)[:60])
            self._flash(UI["red"])
            self.text.delete("1.0", "end")
            self.text.insert("end", f"❌ {res.error}\n", "err")
            self.set_status("调用失败")
            self._beep("warn")
            return

        self.last_payload = payload
        self.b_copy.set_enabled(True)
        self.b_redo.set_enabled(True)
        items = payload.get("items") or [res]
        label, key = CONF_CHIP.get(res.confidence, ("⚪ " + res.confidence, "muted"))
        head = res.answer
        if len(items) > 1:
            head = f"第 1/{len(items)} 题：{res.answer}"
        self.ans.configure(text=head, fg=UI["text"])
        self.chip.configure(text=label, fg=UI.get(key, UI["muted"]),
                            bg=UI.get(key + "_bg", UI["card2"]))
        bits = [f"一致度 {res.agreement:.0%}",
                f"{sum(i.calls for i in items)} 次调用",
                f"{payload['elapsed']:.1f}s",
                str(payload.get("backend", ""))]
        if len(items) > 1:
            bits.insert(0, f"共 {len(items)} 题")
        if payload.get("cached"):
            bits.insert(0, "⚡ 缓存")
        self.meta.configure(text=" · ".join(bits))
        self._flash(UI.get(key, UI["accent"]))

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
        self.text.insert("end", (res.reasoning or "（无）").strip() + "\n")
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
        if (copied and self.cfg.get("refocus", True) and not self.cont_on
                and self.prev_hwnd):
            self.root.after(90, lambda: capture.restore_focus(self.prev_hwnd))
        tail = "  ·  📋 已自动复制" if copied else ""
        qno = qsplit.question_number(payload.get("text") or "")
        qtag = (f"第 {qno[0]}/{qno[1]} 题 · " if qno and qno[1]
                else (f"第 {qno[0]} 题 · " if qno else ""))
        mode = " · 极速" if self.cfg.get("fast") else ""
        self.set_status(f"{qtag}{label}{mode} · 总 {payload['elapsed']:.1f}s{tail}")
        self._beep("done")
        try:
            cfg_mod.save_history(
                payload["text"], format_answer(payload),
                f"reader={payload.get('backend', '')} model={self.model_var.get()}"
                f" conf={res.confidence}")
        except Exception:  # noqa: BLE001
            pass

    def redo(self):
        """重算这一次（跳过缓存，多问几路，置信度低的时候用）。"""
        if self.busy or not self.last_image:
            return
        self.set_status("重算中（跳过缓存）…")
        self._run_path(self.last_image, bypass=True)

    def _render_multi(self, items):
        """一屏多题时，给每题一个「复制」按钮（最多 8 题）。"""
        try:
            for w in self.multi_bar.winfo_children():
                w.destroy()
            if len(items) < 2:
                return
            for i, r in enumerate(items[:8], 1):
                val = r.fill or r.answer
                label = f"{i}·{val[:9]}"
                Pill(self.multi_bar, label,
                     (lambda v=val: self._copy_text(v)),
                     kind="flat", height=28, padx=12, font_size=9
                     ).pack(side="left", padx=(0, 6))
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
        self._copy_text(res.fill or res.answer)

    def clear(self):
        self.text.delete("1.0", "end")
        self.ans.configure(text="按 Ctrl+Alt+Q 圈住题目", fg=UI["dim"])
        self.chip.configure(text="待命", bg=UI["card2"], fg=UI["muted"])
        self.meta.configure(text="")
        self.b_copy.set_enabled(False)
        self.set_status("就绪")

    # ------------------------------------------------------------ 设置
    def open_settings(self):
        bg, card = UI["bg"], UI["card"]
        card2 = UI["card2"]
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=bg)
        win.transient(self.root)
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        frm = tk.Frame(win, bg=bg)
        frm.pack(fill="both", expand=True, padx=18, pady=16)
        win.update_idletasks()
        w, h = 560, 560
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
        for i, (text, var) in enumerate((("窗口置顶", top_var),
                                         ("出答案自动复制", copy_var),
                                         ("分歧追加采样", esc_var),
                                         ("新题/出答案响铃", sound_var),
                                         ("同题不重复作答", dedupe_var),
                                         ("同题缓存秒出", cache_var),
                                         ("一屏多题分别作答", multi_var),
                                         ("答完回到原窗口", refocus_var),
                                         ("极速模式", fast_var))):
            tk.Checkbutton(checks, text=text, variable=var, bg=bg, fg=UI["dim"],
                           selectcolor=card, activebackground=bg,
                           activeforeground=UI["text"], bd=0, highlightthickness=0,
                           font=(FONT, 9)).grid(row=i // 3, column=i % 3,
                                                sticky="w", padx=(0, 16), pady=2)

        row6 = tk.Frame(frm, bg=bg)
        row6.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
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

        msg = tk.Label(frm, text="Key 获取：platform.deepseek.com → API keys",
                       bg=bg, fg=UI["muted"], font=(FONT, 9))
        msg.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

        btns = tk.Frame(frm, bg=bg)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(14, 0))

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
            self.cfg["ocr_quality"] = (
                "high" if qual_var.get().startswith("高精度") else "standard")
            cfg_mod.save(self.cfg)
            self.topmost = self.cfg["always_on_top"]
            try:
                self.root.attributes("-topmost", self.topmost)
            except Exception:  # noqa: BLE001
                pass
            self.lbl_pin.configure(fg=UI["accent"] if self.topmost else UI["muted"])
            self.interval_lbl.configure(text=f"每 {self.cfg['interval']} 秒")
            self.set_status("设置已保存")
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

    def _on_close(self):
        try:
            self.stop_evt.set()
        except Exception:  # noqa: BLE001
            pass
        if self.hk:
            self.hk.stop()
        self.root.destroy()


def main():
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
    app = App(root)
    if args.auto_cont:
        root.after(900, app.start_continuous)
    root.mainloop()


if __name__ == "__main__":
    main()
