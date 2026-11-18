import ctypes, time, tkinter as tk
ctypes.windll.shcore.SetProcessDpiAwareness(2)
from PIL import ImageGrab
from ctypes import wintypes
import app as A
root = tk.Tk(); a = A.App(root); root.update()
a.open_settings(); root.update(); time.sleep(0.8); root.update()
u = ctypes.windll.user32
P = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
found = []
def cb(h, _):
    n = u.GetWindowTextLengthW(h)
    if n:
        b = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, b, n + 1)
        if "设置" in b.value: found.append(h)
    return True
u.EnumWindows(P(cb), 0)
r = wintypes.RECT(); u.GetWindowRect(found[0], ctypes.byref(r))
ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save("_shot_settings3.png")
print("saved", r.right-r.left, r.bottom-r.top)
root.destroy()
