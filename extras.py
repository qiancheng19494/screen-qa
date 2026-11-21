"""桌面小工具：托盘图标、开机自启、自动更新检查。

都只用 Python 标准库 + Windows API（ctypes / winreg），不引第三方依赖——
打包成 exe 时不需要额外 hook，也不会因为少一个包打不开。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

GITHUB_REPO = "qiancheng19494/screen-qa"
APP_REG_NAME = "屏幕问答"
WM_TRAY = 0x0400 + 20


def _def_window_proc(hwnd, msg, wparam, lparam):
    """统一入口：先配好 argtypes，避免 LPARAM 溢出（int too long to convert）。"""
    u = ctypes.WinDLL("user32")
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_longlong
    return u.DefWindowProcW(hwnd, msg, wparam, lparam)  # WM_APP + 20，自己用的私有消息
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ----------------------------------------------------------------- 开机自启
def _launch_command() -> str:
    """开机要执行的命令：打包后是 exe 本身，源码版是 pythonw + app.py。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    here = Path(__file__).resolve().parent
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.is_file() else Path(sys.executable)
    return f'"{exe}" "{here / "app.py"}"'


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_REG_NAME)
            return bool(val)
    except Exception:  # noqa: BLE001
        return False


def _startup_dir() -> Path:
    return (Path(os.environ.get("APPDATA", ""))
            / "Microsoft/Windows/Start Menu/Programs/Startup")


def _startup_bat() -> Path:
    return _startup_dir() / f"{APP_REG_NAME}.bat"


def set_autostart(enable: bool) -> bool:
    """开机自启：先写 HKCU\\...\\Run（够了），被拒再退到「启动」文件夹。"""
    if not enable:
        ok = False
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, APP_REG_NAME)
                    ok = True
                except FileNotFoundError:
                    ok = True
        except Exception:  # noqa: BLE001
            pass
        try:
            bat = _startup_bat()
            if bat.exists():
                bat.unlink()
                ok = True
        except Exception:  # noqa: BLE001
            pass
        return ok
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_REG_NAME, 0, winreg.REG_SZ,
                              _launch_command())
        return True
    except Exception:  # noqa: BLE001
        pass
    try:  # 退路：往「启动」文件夹放一个 bat（同样只影响当前用户）
        bat = _startup_bat()
        bat.parent.mkdir(parents=True, exist_ok=True)
        bat.write_text(f'@echo off\r\nstart "" {_launch_command()}\r\n',
                       encoding="mbcs", errors="ignore")
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- 版本检查
LOCAL_VERSION = "2.6.0"


def check_update(timeout: float = 8.0) -> dict:
    """问 GitHub 要 main 分支最新提交，跟本地版本比较。

    返回 {"ok", "latest", "message", "url", "behind", "error"}；
    网络不通不抛异常（离线时程序照常用）。
    """
    out = {"ok": False, "latest": "", "message": "", "url": "",
           "behind": False, "error": ""}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/main"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "screen-qa", "Accept":
                          "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        sha = str(data.get("sha", ""))[:7]
        msg = str((data.get("commit") or {}).get("message", "")).splitlines()[0]
        out.update(ok=True, latest=sha, message=msg,
                   url=str(data.get("html_url", "")),
                   behind=bool(sha) and sha != LOCAL_VERSION)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ----------------------------------------------------------------- 托盘图标
class TrayIcon(threading.Thread):
    """任务栏通知区图标（Shell_NotifyIconW）。

    左键 = 显示/隐藏窗口，右键 = 菜单（显示/隐藏、自检、退出）。
    回调都在托盘线程里触发，调用方需自己切回 Tk 线程（用 root.after）。
    """

    def __init__(self, icon_path: Path, tooltip: str,
                 on_left: Optional[Callable[[], None]] = None,
                 on_menu: Optional[Callable[[int], None]] = None):
        super().__init__(daemon=True)
        self.icon_path = Path(icon_path)
        self.tooltip = tooltip[:120]
        self.on_left = on_left or (lambda: None)
        self.on_menu = on_menu or (lambda cid: None)
        self.ready = threading.Event()
        self.error = ""
        self.hwnd = 0
        self._stop = False
        self._hinst = 0

    # -- Win32 结构
    class _NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ]

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
    WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
    WM_COMMAND_QUIT = 0x8000 + 3

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                lp = int(lparam) & 0xFFFF
                if lp == self.WM_LBUTTONUP:
                    self.on_left()
                    return 0
                if lp == self.WM_RBUTTONUP:
                    self._popup_menu()
                    return 0
            if msg == 0x0111:  # WM_COMMAND
                self.on_menu(int(wparam) & 0xFFFF)
                return 0
            if msg == 0x0400 + 21:  # WM_APP+21 = 外部请求退出
                self._remove()
                ctypes.windll.user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0010:  # WM_CLOSE
                ctypes.windll.user32.DestroyWindow(hwnd)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return _def_window_proc(hwnd, msg, wparam, lparam)

    def _popup_menu(self):
        u = ctypes.windll.user32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, 0x0000, 1, "显示 / 隐藏")
        u.AppendMenuW(menu, 0x0000, 2, "自检")
        u.AppendMenuW(menu, 0x0800, 0, None)  # MF_SEPARATOR
        u.AppendMenuW(menu, 0x0000, 3, "退出")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)
        cid = u.TrackPopupMenu(menu, 0x0100 | 0x0002, pt.x, pt.y, 0,
                               self.hwnd, None)
        if cid:
            self.on_menu(int(cid))
        u.DestroyMenu(menu)

    def _remove(self):
        try:
            nid = self._nid()
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass

    def _nid(self):
        nid = self._NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(self._NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                 wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
        u.LoadImageW.restype = wintypes.HANDLE
        hicon = u.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x10)  # IMAGE_ICON
        if not hicon:
            try:
                hicon = u.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x10)
            except Exception:  # noqa: BLE001
                hicon = 0
        nid.hIcon = hicon or 0
        nid.szTip = self.tooltip
        return nid

    def run(self):
        try:
            u = ctypes.WinDLL("user32", use_last_error=True)
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            self._hinst = k.GetModuleHandleW(None)
            class_name = f"SQA_TRAY_{os.getpid()}"
            wndproc_t = ctypes.WINFUNCTYPE(
                ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)
            wndproc = wndproc_t(self._wndproc)
            self._wndproc_ref = wndproc

            class _WNDCLASS(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", wndproc_t),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            wc = _WNDCLASS()
            wc.lpfnWndProc = wndproc
            wc.hInstance = self._hinst
            wc.lpszClassName = class_name
            u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
            u.RegisterClassW.restype = wintypes.ATOM
            u.RegisterClassW(ctypes.byref(wc))

            u.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, wintypes.LPVOID]
            u.CreateWindowExW.restype = wintypes.HWND
            # HWND_MESSAGE = -3：只收消息、不显示的窗口
            self.hwnd = u.CreateWindowExW(
                0, class_name, class_name, 0, 0, 0, 0, 0,
                wintypes.HWND(-3), None, self._hinst, None)
            if not self.hwnd:
                self.error = ("CreateWindowEx 失败"
                              f"（WinError {ctypes.get_last_error()}）")
                self.ready.set()
                return
            nid = self._nid()
            if not ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_ADD,
                                                           ctypes.byref(nid)):
                self.error = "Shell_NotifyIcon 失败（任务栏不接受图标）"
            self.ready.set()

            msg = wintypes.MSG()
            while not self._stop:
                got = u.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if got == 0 or got == -1:
                    break
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            self._remove()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()

    def stop(self):
        self._stop = True
        try:
            if self.hwnd:
                ctypes.windll.user32.PostMessageW(self.hwnd, 0x0400 + 21, 0, 0)
        except Exception:  # noqa: BLE001
            pass

# ---------------------------------------------------------------- 语音朗读
def speak(text: str, lang: str = "zh", wait: bool = False) -> bool:
    """用系统自带 SAPI 朗读（不依赖第三方库）。"""
    text = (text or "").strip()
    if not text:
        return False
    text = text.replace('"', "'").replace("`", "'")[:1200]
    voice = "Microsoft Huihui Desktop" if (lang or "zh").startswith("zh") else "Microsoft Zira Desktop"
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"try {{ $s.SelectVoice('{voice}') }} catch {{}}; "
        "$s.Rate = 0; "
        f'$s.Speak("{text}")'
    )
    try:
        flags = 0 if wait else 0x08000000  # CREATE_NO_WINDOW
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         creationflags=flags, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 导出 PDF
def export_pdf(records: list[dict], path, title: str = "屏幕问答 · 作答记录", png_out: str = "") -> str:
    """把作答记录排成 PDF（题目截图 + 答案 + 步骤）。纯 PIL，无需第三方库。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = "C:/Windows/Fonts/msyh.ttc"
    def _font(size):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:  # noqa: BLE001
            return ImageFont.load_default()

    W, H = 1240, 1754           # A4 @150dpi
    margin = 80
    pages = []
    page = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(page)
    f_title, f_h, f_body, f_meta = _font(40), _font(26), _font(24), _font(20)
    y = margin
    d.text((margin, y), title, font=f_title, fill="#111827")
    y += 60
    d.text((margin, y), f"共 {len(records)} 题 · 由「屏幕问答」生成", font=f_meta, fill="#6b7280")
    y += 50

    def wrap(text, font, width):
        lines, cur = [], ""
        for ch in text.replace("\r", ""):
            if ch == "\n":
                lines.append(cur); cur = ""; continue
            if d.textlength(cur + ch, font=font) <= width:
                cur += ch
            else:
                lines.append(cur); cur = ch
        if cur:
            lines.append(cur)
        return lines

    for i, rec in enumerate(records, 1):
        block_h = 260
        if y + block_h > H - margin:
            pages.append(page)
            page = Image.new("RGB", (W, H), "white")
            d = ImageDraw.Draw(page)
            y = margin
        q = (rec.get("question") or rec.get("title") or "").strip()
        ans = str(rec.get("answer") or "").strip()
        fill = str(rec.get("fill") or "").strip()
        conf = str(rec.get("confidence") or "")
        ts = str(rec.get("ts") or "")
        d.text((margin, y), f"{i}. {q[:60]}", font=f_h, fill="#111827")
        y += 36
        meta = f"{ts}  ·  置信度 {conf}  ·  {rec.get('elapsed','?')}s  ·  {rec.get('model','')}"
        d.text((margin, y), meta[:110], font=f_meta, fill="#6b7280")
        y += 30
        for line in wrap(f"答案：{ans}", f_body, W - margin * 2)[:6]:
            d.text((margin, y), line, font=f_body, fill="#111827")
            y += 32
        if fill:
            d.text((margin, y), f"填入：{fill}", font=f_body, fill="#047857")
            y += 32
        img_path = rec.get("shot") or ""
        try:
            if img_path and os.path.exists(img_path):
                im = Image.open(img_path)
                im.thumbnail((W - margin * 2, 420))
                page.paste(im, (margin, y))
                y += im.size[1] + 18
        except Exception:  # noqa: BLE001
            pass
        d.line([(margin, y), (W - margin, y)], fill="#e5e7eb")
        y += 24
    pages.append(page)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    if png_out:
        try:
            pages[0].save(png_out)
        except Exception:  # noqa: BLE001
            pass
    return str(out)

def _app_dir():
    import sys
    from pathlib import Path
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def prepare_update(dest=None, timeout: int = 120) -> dict:
    """把 GitHub 上的 main 分支源码下下来解到 dest（默认 <程序目录>/_update）。

    只下载+解包，不动正在运行的文件；替换动作交给用户/批处理完成。
    """
    import io
    import tarfile
    import urllib.request
    from pathlib import Path

    out_dir = Path(dest) if dest else (_app_dir() / "_update")
    url = "https://codeload.github.com/%s/tar.gz/refs/heads/main" % GITHUB_REPO
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "screen-qa"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "下载失败：%s" % exc}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = [m for m in tar.getmembers()
                       if m.isfile() and ".." not in m.name and not m.name.startswith("/")]
            for m in members:
                parts = m.name.split("/", 1)
                m.name = parts[1] if len(parts) > 1 else parts[0]
            tar.extractall(out_dir, members=members)
        count = len(members)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "解包失败：%s" % exc}
    return {"ok": True, "path": str(out_dir), "files": count, "bytes": len(blob)}
