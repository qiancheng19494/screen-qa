"""启动 app.py 的看门狗：启动失败就把原因弹窗告诉用户，而不是静默什么都不发生。

用法（由 启动.bat 调用）：
    pythonw tools/run_gui.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "_startup_error.log"
PYW = Path(sys.executable).with_name("pythonw.exe")
if not PYW.is_file():
    PYW = Path(sys.executable)


def popup(title: str, text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, text)
        r.destroy()
    except Exception:
        pass


def main() -> int:
    env = os.environ.copy()
    shims = ROOT / "tools" / "cli_shims"
    if shims.is_dir():
        env["PATH"] = str(shims) + os.pathsep + env.get("PATH", "")

    if not (ROOT / "app.py").is_file():
        popup("屏幕问答 · 启动失败", f"缺少 app.py：\n{ROOT}")
        return 1

    if LOG.exists():
        try:
            LOG.unlink()
        except OSError:
            pass

    with open(LOG, "wb") as log:
        proc = subprocess.Popen([str(PYW), str(ROOT / "app.py")], cwd=str(ROOT),
                                stdout=log, stderr=log, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # 3 秒内如果进程没了，说明启动就崩了 —— 把日志尾巴弹出来
    for _ in range(30):
        time.sleep(0.1)
        if proc.poll() is not None:
            break
    else:
        return 0

    time.sleep(0.2)
    tail = ""
    try:
        tail = LOG.read_text(encoding="utf-8", errors="replace").strip()[-1200:]
    except OSError:
        pass
    popup("屏幕问答 · 启动失败",
          f"程序启动后立即退出（退出码 {proc.returncode}）。\n\n"
          f"{tail or '（没有错误输出，可能是被安全软件拦下了）'}\n\n"
          f"日志：{LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
