"""屏幕截图模块。

提供两种方式：
  1. grab_all()            抓取整个（多显示器）桌面
  2. select_region(parent) 弹出半透明遮罩，鼠标拖框选择区域后截图
"""
from __future__ import annotations

import ctypes
from typing import Optional

from PIL import Image, ImageGrab

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def enable_dpi_awareness() -> None:
    """让进程按物理像素工作，否则高分屏下截图会被系统缩放、坐标对不上。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """返回整个虚拟桌面（含所有显示器）的 (x, y, w, h)。"""
    try:
        u = ctypes.windll.user32
        x = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w > 0 and h > 0:
            return int(x), int(y), int(w), int(h)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0, 0, 0


def grab_all() -> Image.Image:
    """抓取整个桌面的截图。"""
    x, y, w, h = virtual_screen_rect()
    if w > 0 and h > 0:
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return ImageGrab.grab(all_screens=True)


def grab_bbox(x: int, y: int, w: int, h: int) -> Image.Image:
    return ImageGrab.grab(bbox=(int(x), int(y), int(x + w), int(y + h)), all_screens=True)


class RegionSelector:
    """全屏半透明遮罩 + 拖拽选框。

    用法：
        sel = RegionSelector(root)
        img = sel.select()      # 阻塞，返回 PIL.Image 或 None（取消）
    """

    def __init__(self, root):
        self.root = root
        self._start = None
        self._rect_id = None
        self._result: Optional[tuple[int, int, int, int]] = None
        self.bbox: Optional[tuple[int, int, int, int]] = None  # 绝对坐标 (x, y, w, h)
        self.vx, self.vy, self.vw, self.vh = virtual_screen_rect()

    def select(self) -> Optional[Image.Image]:
        import tkinter as tk

        vx, vy, vw, vh = self.vx, self.vy, self.vw, self.vh
        if vw <= 0 or vh <= 0:
            return None

        # 先截图，再显示遮罩，避免把遮罩本身截进去
        shot = grab_all()

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        try:
            top.attributes("-alpha", 0.28)
        except Exception:  # noqa: BLE001
            pass
        top.configure(bg="black")
        top.focus_force()
        top.grab_set()

        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        hint = canvas.create_text(
            vw // 2, 60,
            text="拖拽选择题目区域　·　Esc 取消　·　Enter 全屏",
            fill="#ffffff", font=("Microsoft YaHei UI", 16),
        )
        canvas.create_rectangle(
            vw // 2 - 300, 38, vw // 2 + 300, 84, outline="#4da3ff", width=1
        )

        def on_press(ev):
            self._start = (ev.x, ev.y)
            if self._rect_id:
                canvas.delete(self._rect_id)
            self._rect_id = canvas.create_rectangle(
                ev.x, ev.y, ev.x, ev.y, outline="#4da3ff", width=2
            )

        def on_drag(ev):
            if not self._start or not self._rect_id:
                return
            x0, y0 = self._start
            canvas.coords(self._rect_id, x0, y0, ev.x, ev.y)

        def on_release(ev):
            if not self._start:
                return
            x0, y0 = self._start
            x1, y1 = ev.x, ev.y
            left, right = sorted((x0, x1))
            top_y, bottom_y = sorted((y0, y1))
            if right - left < 8 or bottom_y - top_y < 8:
                self._result = None
            else:
                self._result = (left, top_y, right - left, bottom_y - top_y)
            self._close(top)

        def on_escape(_ev=None):
            self._result = None
            self._close(top)

        def on_enter(_ev=None):
            self._result = (0, 0, vw, vh)
            self._close(top)

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_escape)
        top.bind("<Return>", on_enter)
        canvas.focus_set()

        self.root.wait_window(top)

        if not self._result:
            return None
        left, top_y, w, h = self._result
        self.bbox = (self.vx + left, self.vy + top_y, w, h)
        return shot.crop((left, top_y, left + w, top_y + h))

    @staticmethod
    def _close(top) -> None:
        try:
            top.grab_release()
        except Exception:  # noqa: BLE001
            pass
        try:
            top.destroy()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 焦点往返
def foreground_window() -> int:
    """当前前台窗口的顶层句柄（0 表示拿不到）。"""
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            return 0
        root = int(ctypes.windll.user32.GetAncestor(hwnd, 2))  # GA_ROOT
        return root or hwnd
    except Exception:  # noqa: BLE001
        return 0


def restore_focus(hwnd: int) -> bool:
    """把焦点还给之前的窗口。

    Windows 默认不让后台进程抢前台，所以先用 AttachThreadInput 挂到
    目标线程上再 SetForegroundWindow，完事再摘掉。
    """
    if not hwnd:
        return False
    u = ctypes.windll.user32
    try:
        if int(u.GetForegroundWindow()) == hwnd:
            return True
        target_tid = int(u.GetWindowThreadProcessId(hwnd, None))
        cur_tid = int(ctypes.windll.kernel32.GetCurrentThreadId())
        attached = False
        if target_tid and target_tid != cur_tid:
            attached = bool(u.AttachThreadInput(cur_tid, target_tid, True))
        try:
            u.SetForegroundWindow(hwnd)
            u.BringWindowToTop(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(cur_tid, target_tid, False)
        return int(u.GetForegroundWindow()) == hwnd
    except Exception:  # noqa: BLE001
        return False
