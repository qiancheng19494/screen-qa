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
import queue
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
import reader  # noqa: E402
from deepseek_api import DeepSeekClient  # noqa: E402
from solver import Solver  # noqa: E402

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


# ---------------------------------------------------------------- 核心流程
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
    emit("log", f"识别完成（{rr.backend}，{len(rr.text)} 字，{rr.elapsed:.1f}s）")

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
    emit("log", "DeepSeek 思考中…")
    res = solver.solve(rr.text)

    return {
        "ok": True,
        "text": rr.text,
        "backend": rr.backend,
        "read_elapsed": rr.elapsed,
        "result": res,
        "elapsed": time.time() - t0,
        "warnings": rr.warnings,
    }


def format_answer(payload: dict) -> str:
    res = payload["result"]
    if res.answer.startswith("（调用失败）"):
        return f"❌ 调用 DeepSeek 失败：{res.error}"
    icon = {"高": "🟢", "中": "🟡", "存疑": "🔴"}.get(res.confidence, "⚪")
    lines = [
        f"【最终答案】{res.answer}",
        f"{icon} 置信度：{res.confidence}（样本一致度 {res.agreement:.0%}，"
        f"{res.calls} 次模型调用，{res.elapsed:.1f}s）",
        "",
        "—— 详细推理 ——",
        (res.reasoning or "（无）").strip(),
    ]
    if len(res.samples) > 1:
        lines += ["", "—— 各次采样 ——"]
        lines += [f"{i}. {s}" for i, s in enumerate(res.samples, 1)]
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
        self.last_image = None
        self.cont_on = False
        self.cont_region = self._load_region()
        self.stop_evt = threading.Event()
        self.cont_sig = ""
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

        if not self.cfg.get("api_key"):
            self.set_status("⚠ 还没填 API Key —— 点右上角 ⚙")
        else:
            self.set_status("就绪 · 按 Ctrl+Alt+Q 圈题")

    # ------------------------------------------------------------ 窗口
    def _init_window(self):
        root = self.root
        root.title(APP_TITLE)
        root.configure(bg=UI["bg"])
        place_bottom_right(root, 474, 668)
        root.minsize(430, 540)
        self.topmost = bool(self.cfg.get("always_on_top", True))
        root.attributes("-topmost", self.topmost)

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
        self.b_copy = Pill(bar, "复制答案", self.copy_answer, kind="ghost", width=84)
        self.b_copy.pack(side="right")
        self.b_copy.set_enabled(False)

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
        self.busy = True
        self._set_buttons(False)
        self.set_status("处理中…")
        self._set_pulse(True)

        cfg = self._current_cfg()

        def emit(kind, payload):
            self.q.put((kind, payload))

        def work():
            try:
                payload = run_pipeline(str(tmp), cfg, emit)
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
                    else:
                        self.shot_full()
            except queue.Empty:
                pass
            except Exception:  # noqa: BLE001
                pass

        self.root.after(110, self._poll)

    def _setup_hotkeys(self):
        try:
            import hotkey

            self.hk = hotkey.HotkeyManager()
            self.hk.add("region", self.cfg.get("hotkey_region", "ctrl+alt+q"))
            self.hk.add("full", self.cfg.get("hotkey_full", "ctrl+alt+w"))
            if not self.hk.start():
                self.log(f"⚠ 全局热键不可用：{self.hk.error}（按钮仍可用）")
        except Exception as exc:  # noqa: BLE001
            self.log(f"⚠ 热键初始化失败：{exc}")

    # ------------------------------------------------------------ 结果渲染
    def _finish(self, payload):
        self.busy = False
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
            return

        self.last_payload = payload
        self.b_copy.set_enabled(True)
        label, key = CONF_CHIP.get(res.confidence, ("⚪ " + res.confidence, "muted"))
        self.ans.configure(text=res.answer, fg=UI["text"])
        self.chip.configure(text=label, fg=UI.get(key, UI["muted"]),
                            bg=UI.get(key + "_bg", UI["card2"]))
        self.meta.configure(
            text=f"一致度 {res.agreement:.0%} · {res.calls} 次调用 · "
                 f"{payload['elapsed']:.1f}s · {payload.get('backend', '')}")
        self._flash(UI.get(key, UI["accent"]))

        self.text.delete("1.0", "end")
        for w in payload.get("warnings") or []:
            self.text.insert("end", f"⚠ {w}\n", "warn")
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
        if self.cfg.get("auto_copy") and res.answer and "调用失败" not in res.answer:
            copied = self._copy_text(res.answer, quiet=True)
        tail = "  ·  📋 已自动复制" if copied else ""
        self.set_status(f"{label} · 总 {payload['elapsed']:.1f}s{tail}")
        try:
            cfg_mod.save_history(
                payload["text"], format_answer(payload),
                f"reader={payload.get('backend', '')} model={self.model_var.get()}"
                f" conf={res.confidence}")
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
        self._copy_text(self.last_payload["result"].answer)

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
        w, h = 560, 500
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
            value="高精度（更准，慢约 1 秒）" if str(self.cfg.get("ocr_quality", "high")) == "high"
            else "标准（更快）")
        qual = tk.OptionMenu(frm, qual_var, "高精度（更准，慢约 1 秒）", "标准（更快）")
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
        for text, var in (("窗口置顶", top_var),
                          ("出答案自动复制", copy_var),
                          ("分歧时追加采样（更准）", esc_var)):
            tk.Checkbutton(checks, text=text, variable=var, bg=bg, fg=UI["dim"],
                           selectcolor=card, activebackground=bg,
                           activeforeground=UI["text"], bd=0, highlightthickness=0,
                           font=(FONT, 9)).pack(side="left", padx=(0, 18))

        row6 = tk.Frame(frm, bg=bg)
        row6.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(row6, text=self._region_text(), bg=bg, fg=UI["muted"],
                 font=(FONT, 9)).pack(side="left")

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
