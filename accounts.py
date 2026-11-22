"""本机账号：注册 / 登录 / 游客。

为什么是本机的：这个程序没有服务器（也刻意不连服务器），所以要"注册登录"
只能用本机账号——作用是**多人共用一台电脑时隔离数据**（各自的作答记录、
错题本、缓存、历史），并给每份配置独立的 API Key。

- 密码用 PBKDF2-SHA256（12 万次迭代 + 16 字节随机盐）存 `users.json`；
- 数据目录：`data/<用户名>/`；游客用根目录（旧版数据所在处）；
- 没有任何联网校验，删掉 `users.json` 就等于重置。
"""
import hashlib
import json
import re
import secrets
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USERS = ROOT / "users.json"
ITER = 120_000

# 登录窗自己的配色（不 import app，避免循环依赖）
BG, CARD, BORDER = "#0a0c10", "#131820", "#232b38"
TEXT, DIM, MUTED = "#e9ecf2", "#98a2b3", "#6a7486"
ACCENT, RED, GREEN = "#4d9dff", "#ff6b6b", "#3ddc84"
FONT = "Microsoft YaHei UI"


def _load() -> dict:
    try:
        return json.loads(USERS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    USERS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                               bytes.fromhex(salt), ITER).hex()


def safe_name(user: str) -> str:
    """用户名 → 可做目录名的字符串（中文保留，去掉路径分隔符等）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", user.strip())
    return s.strip("_")[:40] or "user"


def users() -> list[str]:
    return sorted(_load().keys())


def exists(user: str) -> bool:
    return user in _load()


def register(user: str, pw: str) -> str | None:
    """成功返回 None，失败返回给用户看的中文原因。"""
    user = (user or "").strip()
    if len(user) < 2:
        return "用户名至少 2 个字符"
    if len(user) > 20:
        return "用户名最多 20 个字符"
    if len(pw or "") < 6:
        return "密码至少 6 位"
    data = _load()
    if user in data:
        return "这个用户名已经注册过了"
    salt = secrets.token_hex(16)
    data[user] = {"salt": salt, "hash": _hash(pw, salt), "created": int(time.time())}
    _save(data)
    return None


def verify(user: str, pw: str) -> bool:
    rec = _load().get((user or "").strip())
    if not rec:
        return False
    return secrets.compare_digest(rec.get("hash", ""), _hash(pw or "", rec.get("salt", "")))


def change_password(user: str, old: str, new: str) -> str | None:
    if not verify(user, old):
        return "原密码不对"
    if len(new or "") < 6:
        return "新密码至少 6 位"
    data = _load()
    salt = secrets.token_hex(16)
    data[user] = {"salt": salt, "hash": _hash(new, salt),
                  "created": int(data.get(user, {}).get("created", time.time()))}
    _save(data)
    return None


def delete(user: str, pw: str) -> str | None:
    if not verify(user, pw):
        return "密码不对，不能删"
    data = _load()
    data.pop(user, None)
    _save(data)
    return None


def ask_login(root: tk.Tk) -> str | None:
    """登录窗。返回用户名、游客返回 ""，点关闭返回 None。"""
    win = tk.Toplevel(root)
    win.title("屏幕问答 · 登录")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    w, h = 380, 398
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

    result = {"user": None}

    tk.Label(win, text="屏幕问答", bg=BG, fg=TEXT,
             font=(FONT, 16, "bold")).pack(anchor="w", padx=22, pady=(20, 2))
    tk.Label(win, text="本机账号 · 数据只存在这台电脑", bg=BG, fg=MUTED,
             font=(FONT, 9)).pack(anchor="w", padx=22)

    form = tk.Frame(win, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    form.pack(fill="x", padx=22, pady=14)

    def field(label: str, show: str | None = None) -> tk.Entry:
        tk.Label(form, text=label, bg=CARD, fg=DIM,
                 font=(FONT, 9)).pack(anchor="w", padx=14, pady=(10, 2))
        e = tk.Entry(form, bg="#0d1117", fg=TEXT, insertbackground=TEXT,
                     relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT,
                     font=(FONT, 10), show=show or "")
        e.pack(fill="x", padx=14, ipady=5)
        return e

    e_user = field("用户名")
    e_pw = field("密码", "●")
    tip = tk.Label(form, text="", bg=CARD, fg=RED, font=(FONT, 8),
                   wraplength=300, justify="left")
    tip.pack(anchor="w", padx=14, pady=(6, 10))

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", padx=22)

    def make(text: str, cmd, color=ACCENT, dark_text=True):
        b = tk.Label(btns, text=text, bg=color, fg="#0a0c10" if dark_text else TEXT,
                     font=(FONT, 10, "bold"), padx=12, pady=6, cursor="hand2")
        b.bind("<Button-1>", lambda _e: cmd())
        return b

    def do_login():
        u, p = e_user.get().strip(), e_pw.get()
        if not exists(u):
            tip.configure(text="没有这个用户（第一次用请点「注册」）", fg=RED)
            return
        if not verify(u, p):
            tip.configure(text="密码不对", fg=RED)
            return
        result["user"] = u
        win.destroy()

    def do_register():
        u, p = e_user.get().strip(), e_pw.get()
        err = register(u, p)
        if err:
            tip.configure(text=err, fg=RED)
            return
        tip.configure(text=f"已注册「{u}」，正在进入…", fg=GREEN)
        win.after(500, do_login)

    def do_guest():
        result["user"] = ""
        win.destroy()

    make("登录", do_login).pack(side="left")
    make("注册", do_register, color=CARD, dark_text=False).pack(side="left", padx=8)
    make("游客进入", do_guest, color=CARD, dark_text=False).pack(side="right")

    tk.Label(win, text="密码只做本机校验、不上传；忘记密码可删 users.json 重置",
             bg=BG, fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=22, pady=(8, 10))

    e_user.focus_set()
    win.bind("<Return>", lambda _e: do_login())
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.grab_set()
    root.wait_window(win)
    return result["user"]
