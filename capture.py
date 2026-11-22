"""屏幕截图模块。

提供两种方式：
  1. grab_all()            抓取整个（多显示器）桌面
  2. select_region(parent) 弹出半透明遮罩，鼠标拖框选择区域后截图
"""
from __future__ import annotations

import ctypes
import time
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


# ---------------------------------------------------------------- 键盘注入
_VK_CONTROL = 0x11
_VK_V = 0x56
_VK_RETURN = 0x0D
_KEYEVENTF_KEYUP = 0x0002
_INPUT_INJECTED = None


def _tap(vk: int, hold: float = 0.03) -> None:
    u = ctypes.windll.user32
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(hold)
    u.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def send_paste(enter: bool = False, settle: float = 0.12) -> bool:
    """在当前前台窗口按一次 Ctrl+V（可选再回车）。

    用途：答完之后直接把答案填进 MyMathLab / Canvas 的输入框，
    省掉「手动切窗口 + Ctrl+V」这一步。注入失败（例如宿主禁止合成按键）
    只返回 False，绝不抛异常。
    """
    global _INPUT_INJECTED
    u = ctypes.windll.user32
    try:
        time.sleep(max(0.0, settle))
        u.keybd_event(_VK_CONTROL, 0, 0, 0)
        time.sleep(0.02)
        u.keybd_event(_VK_V, 0, 0, 0)
        time.sleep(0.03)
        u.keybd_event(_VK_V, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)
        u.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)
        ok = True
        if enter:
            time.sleep(0.25)
            _tap(_VK_RETURN)
        _INPUT_INJECTED = ok
        return ok
    except Exception:  # noqa: BLE001
        _INPUT_INJECTED = False
        return False

# ---------------------------------------------------------------- 剪贴板
CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_DIBV5 = 17


def clipboard_sequence() -> int:
    """剪贴板变更计数（每次有新内容都会 +1）。"""
    try:
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001
        return 0


def _u32():
    u = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.IsClipboardFormatAvailable.restype = ctypes.c_int
    u.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    return u, k32


def _open_clipboard(retries: int = 6, wait: float = 0.05) -> bool:
    u = ctypes.windll.user32
    for _ in range(max(1, retries)):
        if u.OpenClipboard(None):
            return True
        time.sleep(wait)
    return False


def clipboard_text() -> str:
    """读剪贴板里的纯文字（拿不到返回空串）。"""
    u, k32 = _u32()
    if not _open_clipboard():
        return ""
    try:
        if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = k32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(h)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            u.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass


def clipboard_has_image() -> bool:
    u = ctypes.windll.user32
    if not _open_clipboard():
        return False
    try:
        return bool(u.IsClipboardFormatAvailable(CF_DIB) or
                    u.IsClipboardFormatAvailable(CF_BITMAP) or
                    u.IsClipboardFormatAvailable(CF_DIBV5))
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            u.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass


def clipboard_image() -> Optional[Image.Image]:
    """把剪贴板里的图（Win+Shift+S / 微信截图）取成 PIL.Image。"""
    try:
        if not clipboard_has_image():
            return None
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
        return None
    except Exception:  # noqa: BLE001
        return None


def clipboard_set_text(text: str) -> bool:
    """把文字写进剪贴板（自己用，省得依赖 tk 的 clipboard 生命期问题）。"""
    u, k32 = _u32()
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        u.EmptyClipboard()
        h = k32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            k32.GlobalUnlock(h)
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            u.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 按窗口截图
def list_windows(min_size: int = 200) -> list[tuple[int, str]]:
    """列出可见的顶层窗口（跳过极小窗口/无标题窗口）。"""
    u = ctypes.windll.user32
    out: list[tuple[int, str]] = []
    P = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        try:
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            r = (ctypes.c_long * 4)()
            u.GetWindowRect(hwnd, r)
            w, h = r[2] - r[0], r[3] - r[1]
            if w < min_size or h < min_size:
                return True
            if title in ("Program Manager", "Windows 输入体验"):
                return True
            out.append((int(hwnd), title[:80]))
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        u.EnumWindows(P(cb), 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def window_title(hwnd: int) -> str:
    try:
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def grab_window(hwnd: int) -> Optional[Image.Image]:
    """抓指定窗口的内容（PrintWindow，被别的窗口遮挡也能抓到）。"""
    from PIL import Image as _Image

    u = ctypes.windll.user32
    g32 = ctypes.windll.gdi32
    try:
        rect = (ctypes.c_long * 4)()
        u.GetWindowRect(hwnd, rect)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w <= 0 or h <= 0:
            return None
        hdc = u.GetWindowDC(hwnd)
        mdc = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, w, h)
        old = g32.SelectObject(mdc, bmp)
        ok = u.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
        if not ok:
            u.PrintWindow(hwnd, mdc, 0)
        class _BIH(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]
        bi = _BIH()
        bi.biSize = ctypes.sizeof(_BIH)
        bi.biWidth = w
        bi.biHeight = -h
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = _Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        g32.SelectObject(mdc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u.ReleaseDC(hwnd, hdc)
        return img
    except Exception:  # noqa: BLE001
        return None

def autocrop(img, tol=20, pad=6, max_side=420):
    """裁掉四周纯色留白（整屏截图常见大片空白），返回裁好的图。"""
    try:
        from collections import Counter
        im = img.convert("RGB")
        w, h = im.size
        if w < 80 or h < 60:
            return img
        scale = min(1.0, max_side / float(w))
        sw, sh = max(2, int(w * scale)), max(2, int(h * scale))
        small = im.resize((sw, sh))
        px = small.load()
        cnt = Counter()
        for x in range(0, sw, max(1, sw // 50)):
            cnt[px[x, 0]] += 1
            cnt[px[x, sh - 1]] += 1
        for y in range(0, sh, max(1, sh // 50)):
            cnt[px[0, y]] += 1
            cnt[px[sw - 1, y]] += 1
        bg = cnt.most_common(1)[0][0]

        def same(c):
            return (abs(c[0] - bg[0]) <= tol and abs(c[1] - bg[1]) <= tol
                    and abs(c[2] - bg[2]) <= tol)

        left, top, right, bottom = sw, sh, -1, -1
        for y in range(sh):
            for x in range(sw):
                if not same(px[x, y]):
                    if x < left:
                        left = x
                    if x > right:
                        right = x
                    if y < top:
                        top = y
                    if y > bottom:
                        bottom = y
        if right <= left or bottom <= top:
            return img
        if left <= 1 and top <= 1 and right >= sw - 2 and bottom >= sh - 2:
            return img
        inv = 1.0 / scale
        l = max(0, int(left * inv) - pad)
        t = max(0, int(top * inv) - pad)
        r = min(w, int((right + 1) * inv) + pad)
        b = min(h, int((bottom + 1) * inv) + pad)
        if (r - l) < 120 or (b - t) < 60:
            return img
        return im.crop((l, t, r, b))
    except Exception:  # noqa: BLE001
        return img
