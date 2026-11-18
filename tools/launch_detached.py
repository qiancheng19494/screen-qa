"""在后台把界面启动起来，然后自己立刻退出（不占控制台）。

用处：从脚本 / 终端 / DSH 的 bash 里启动 GUI 时，如果直接跑 `python app.py`，
父进程一退出窗口就跟着没了。这里用 CREATE_BREAKAWAY_FROM_JOB + DETACHED_PROCESS
把子进程从当前作业对象里摘出去，父进程退出后窗口继续活着。

用法：
    python tools/launch_detached.py            # 启动界面
    python tools/launch_detached.py --cli      # 走命令行模式（前景，直接看输出）

失败时会打印 Windows 错误码，退回 `explorer.exe` 方式再试一次。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _shell_execute(exe: str, args: str) -> int:
    """交给 Windows 外壳去启动：新进程的父级是 explorer，不属于当前作业对象，
    所以父进程（这个脚本 / 终端 / 编辑器）退出后窗口仍然活着。"""
    return int(ctypes.windll.shell32.ShellExecuteW(
        None, "open", exe, args, str(ROOT), 1))


def launch() -> int:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        rc = _shell_execute(str(pythonw), f'"{APP}"')
        print(f"ShellExecute(pythonw) 返回 {rc}")
        if rc > 32:
            print("已启动界面（窗口贴在屏幕右下角）")
            return 0

    bat = ROOT / "启动.bat"
    if bat.exists():
        rc = _shell_execute(str(bat), "")
        print(f"ShellExecute(启动.bat) 返回 {rc}")
        if rc > 32:
            print("已启动界面（通过 启动.bat）")
            return 0

    # 最后手段：Popen 直起（父进程退出后会跟着没，只在临时调试时有用）
    print("外壳启动失败，退回 Popen…")
    log = open(ROOT / "_run.log", "ab")
    try:
        p = subprocess.Popen(
            [sys.executable, str(APP)], cwd=str(ROOT), stdout=log, stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=(CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS
                           | CREATE_NEW_PROCESS_GROUP),
            close_fds=True)
        print(f"已启动（可能随父进程退出），pid={p.pid}")
        return 0
    except OSError as exc:
        print(f"启动失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(launch())
