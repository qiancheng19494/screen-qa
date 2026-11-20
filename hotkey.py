"""Windows 全局热键（ctypes 调用 RegisterHotKey，无需第三方库）。

注意：RegisterHotKey 只能在同一线程里注册并等待消息，
所以这里用独立线程跑 GetMessage 循环，触发时把事件丢进队列由 UI 处理。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import queue
import threading

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

_MODS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

_VK_SPECIAL = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "print": 0x2C, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "esc": 0x1B, "enter": 0x0D,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """"ctrl+alt+q" → (mods, vk)。"""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    mods = MOD_NOREPEAT
    vk = 0
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif p in _VK_SPECIAL:
            vk = _VK_SPECIAL[p]
        elif len(p) == 1:
            vk = ord(p.upper())
        else:
            raise ValueError(f"无法识别的按键: {p}")
    if vk == 0:
        raise ValueError(f"热键缺少主键: {spec}")
    return mods, vk


class HotkeyManager:
    """注册若干个全局热键；触发的事件进入 self.events（queue.Queue）。

    支持"退路"：首选键被别的软件占用时自动换下一个，并把实际生效的组合
    记在 self.bound 里（界面上要显示真实生效的键，不能骗用户）。
    """

    def __init__(self):
        self.events: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pending: dict[str, list[str]] = {}
        self._registered: dict[int, str] = {}
        self.failed: list[str] = []
        self.bound: dict[str, str] = {}
        self.ok_count = 0
        self._next_id = 1
        self._ready = threading.Event()
        self.error = ""

    def add(self, name: str, spec: str, fallbacks=()) -> bool:
        specs = [spec] + [s for s in fallbacks if s and s != spec]
        self._pending[name] = specs
        return True

    def start(self) -> bool:
        if not self._pending:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")
        self._thread.start()
        self._ready.wait(3.0)
        # 只要有一个注册成功就算可用；失败的写在 self.failed 里由调用方提示
        return self.ok_count > 0

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        for name, specs in self._pending.items():
            for spec in specs:
                try:
                    mods, vk = parse_hotkey(spec)
                except Exception:  # noqa: BLE001
                    continue
                hid = self._next_id
                self._next_id += 1
                if user32.RegisterHotKey(None, hid, mods, vk):
                    self._registered[hid] = name
                    self.bound[name] = spec
                    self.ok_count += 1
                    break
            else:
                self.failed.append(name)
                self.error = self.error or f"热键全被占用: {name}（{specs[0]}）"
        self._ready.set()
        if self.ok_count == 0:
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                name = self._registered.get(int(msg.wParam))
                if name:
                    self.events.put(name)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        for hid in list(self._registered):
            try:
                ctypes.windll.user32.UnregisterHotKey(None, hid)
            except Exception:  # noqa: BLE001
                pass
        self._registered.clear()
