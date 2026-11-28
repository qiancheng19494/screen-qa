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
WIN = "#080b10"
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
    """登录/注册窗（自绘卡片，深色 + 渐变 + Tab 切换）。返回用户名、游客返回 ""，关闭返回 None。"""
    import math
    import sys

    win = tk.Toplevel(root)
    win.title("屏幕问答 · 登录")
    win.configure(bg=WIN)
    win.resizable(False, False)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass
    W, H = 440, 520
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw - W) // 2}+{max(36, (sh - H) // 3)}")

    ui_font = "Microsoft YaHei UI" if sys.platform == "win32" else "PingFang SC"
    mono_like = ui_font

    c = tk.Canvas(win, width=W, height=H, bg=WIN, highlightthickness=0, bd=0)
    c.pack(fill="both", expand=True)

    result = {"user": None}
    state = {"mode": "login"}
    entries: dict[str, tk.Entry] = {}

    # ---------- 小工具 ----------
    def mix(c1: str, c2: str, t: float) -> str:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(a[k] + (b[k] - a[k]) * t):02x}" for k in range(3))

    def rr(x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, splinesteps=24, **kw)

    def rr_grad(x1, y1, x2, y2, r, c1, c2, steps=90, tag=""):
        """圆角矩形内的竖向渐变（逐行画，两端按圆角收边）。"""
        for i in range(steps):
            t = i / max(1, steps - 1)
            y = y1 + (y2 - y1) * t
            dy_top, dy_bot = y - y1, y2 - y
            inset = 0.0
            if dy_top < r:
                inset = r - math.sqrt(max(0.0, r * r - (r - dy_top) ** 2))
            if dy_bot < r:
                inset = max(inset, r - math.sqrt(max(0.0, r * r - (r - dy_bot) ** 2)))
            c.create_line(x1 + inset, y, x2 - inset, y, fill=mix(c1, c2, t), tags=tag)

    def rrect_outline(x1, y1, x2, y2, r, color, width=1, tag=""):
        return rr(x1, y1, x2, y2, r, fill="", outline=color, width=width, tags=tag)

    # ---------- 背景 / 卡片 ----------
    rr_grad(24, 26, 416, 500, 20, "#141c28", "#0f1620")
    rrect_outline(24, 26, 416, 500, 20, "#26313f")

    rr(26, 28, 414, 498, 19, fill="#101722", outline="")
    rr_grad(26, 28, 414, 498, 19, "#161f2d", "#0e141d")
    # 顶部一条很淡的蓝色渐光线，代替高光
    for i in range(180):
        t = i / 179
        x0 = 48 + (392 - 48) * t
        c.create_line(x0, 28.5, x0 + 1, 28.5,
                      fill=mix("#141f2f", "#2a4766", abs(t - 0.5) * 2), width=1)

    # ---------- Logo ----------
    rr_grad(48, 52, 96, 100, 14, "#6fb4ff", "#2f6fd0")
    rrect_outline(48, 52, 96, 100, 14, "#8ec3ff", width=1)
    # 屏幕图标（白色描边）
    c.create_line(60, 68, 84, 68, fill="#ffffff", width=2, capstyle="round")
    c.create_line(60, 68, 60, 82, fill="#ffffff", width=2, capstyle="round")
    c.create_line(60, 82, 84, 82, fill="#ffffff", width=2, capstyle="round")
    c.create_line(84, 68, 84, 82, fill="#ffffff", width=2, capstyle="round")
    c.create_line(72, 86, 72, 90, fill="#ffffff", width=2, capstyle="round")
    c.create_text(72, 77, text="?", fill="#0b2a4d", font=(ui_font, 10, "bold"))

    c.create_text(112, 62, text="屏幕问答", anchor="w", fill="#eaf1fb",
                  font=(ui_font, 17, "bold"))
    c.create_text(112, 86, text="本机账号 · 数据只留在这台电脑", anchor="w", fill="#8b97a9",
                  font=(ui_font, 9))

    # ---------- Tab ----------
    tabs = {"login": (52, 128, 218, 168), "register": (222, 128, 388, 168)}
    c.create_polygon([56, 128, 214, 128, 218, 132, 218, 164, 214, 168, 56, 168,
                      52, 164, 52, 132], smooth=True, fill="#0b121b", outline="#26313f")
    tab_items = {}

    def draw_tabs():
        c.delete("tabfill")
        for name, box in tabs.items():
            x1, y1, x2, y2 = box
            for it in tab_items.pop(name, []):
                c.delete(it)
            on = state["mode"] == name
            if on:
                rr_grad(x1, y1, x2, y2, 9, "#2f5f9e", "#24476f", tag="tabfill")
                rrect_outline(x1, y1, x2, y2, 9, "#4d8fd6", tag="tabfill")
            color = "#eaf4ff" if on else "#8b97a9"
            it = c.create_text((x1 + x2) / 2, (y1 + y2) / 2,
                               text="登录" if name == "login" else "注册",
                               fill=color, font=(ui_font, 12, "bold"))
            tab_items[name] = [it]
            pass
            c.tag_bind(it, "<Enter>", lambda _e, n=name: c.configure(cursor="hand2"))
            c.tag_bind(it, "<Leave>", lambda _e, n=name: c.configure(cursor=""))

    # ---------- 输入框 ----------
    def make_field(y: int, key: str, label: str, icon: str) -> tk.Entry:
        x1, x2 = 48, 392
        c.create_text(x1 + 2, y - 12, text=label, anchor="w", fill="#8b97a9",
                      font=(ui_font, 9))
        box = rr(x1, y, x2, y + 46, 11, fill="#0c131d", outline="#2a3646", width=1)
        # 左侧图标（矢量）
        ix, iy = x1 + 22, y + 23
        if icon == "user":
            c.create_oval(ix - 5, iy - 10, ix + 5, iy, outline="#7f8ea6", width=1.6)
            c.create_arc(ix - 9, iy - 1, ix + 9, iy + 17, start=0, extent=180,
                         style="arc", outline="#7f8ea6", width=1.6)
        else:
            c.create_rectangle(ix - 8, iy - 4, ix + 8, iy + 9, outline="#7f8ea6", width=1.6)
            c.create_arc(ix - 5, iy - 12, ix + 5, iy - 1, start=0, extent=180,
                         style="arc", outline="#7f8ea6", width=1.6)
        e = tk.Entry(win, bg="#0c131d", fg=TEXT, insertbackground=TEXT, relief="flat",
                     bd=0, highlightthickness=0, font=(ui_font, 12),
                     show="●" if key == "pw" else "")
        c.create_window(x1 + 46, y + 23, window=e, width=252, height=24, anchor="w")
        e.bind("<FocusIn>", lambda _e, b=box: c.itemconfigure(b, outline="#4d9dff"))
        e.bind("<FocusOut>", lambda _e, b=box: c.itemconfigure(b, outline="#2a3646"))
        e.bind("<Return>", lambda _e: submit())
        entries[key] = e
        return e

    make_field(190, "user", "用户名", "user")
    make_field(262, "pw", "密码", "lock")

    # 眼睛（显示/隐藏密码）
    eye = {"on": False}

    def draw_eye():
        for tag in ("eyeOpen", "eyeClosed"):
            c.delete(tag)
        x, y = 368, 285
        if eye["on"]:
            c.create_oval(x - 10, y - 6, x + 10, y + 6, outline="#8fb9f0", width=1.5, tags="eyeOpen")
            c.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#8fb9f0", outline="", tags="eyeOpen")
        else:
            c.create_oval(x - 10, y - 6, x + 10, y + 6, outline="#7f8ea6", width=1.5, tags="eyeClosed")
            c.create_line(x - 9, y - 6, x + 9, y + 6, fill="#7f8ea6", width=1.5, tags="eyeClosed")

    def toggle_eye(_e=None):
        eye["on"] = not eye["on"]
        try:
            entries["pw"].configure(show="" if eye["on"] else "●")
        except tk.TclError:
            pass
        draw_eye()

    draw_eye()

    # ---------- 提示 / 主按钮 / 游客 ----------
    tip = c.create_text(220, 322, text="", fill=RED, font=(ui_font, 9), width=344)

    btn = {}
    btn_items = {}

    def draw_button():
        x1, y1, x2, y2 = 48, 336, 392, 384
        for it in btn_items.pop("all", []):
            c.delete(it)
        hot = btn.get("hot", False)
        rr_grad(x1, y1, x2, y2, 12,
                "#79baff" if hot else "#6fb4ff", "#2f7df6" if not hot else "#3b8cff")
        rrect_outline(x1, y1, x2, y2, 12, "#9ccbff", width=1)
        label = "登录" if state["mode"] == "login" else "创建账号"
        txt = c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=label, fill="#06101f",
                            font=(ui_font, 12, "bold"))
        btn_items["all"] = [txt]
        for it in btn_items["all"]:
            c.tag_bind(it, "<Button-1>", lambda _e: submit())
            c.tag_bind(it, "<Enter>", lambda _e: (btn.update(hot=True), draw_button(),
                                                  c.configure(cursor="hand2")))
            c.tag_bind(it, "<Leave>", lambda _e: (btn.update(hot=False), draw_button(),
                                                  c.configure(cursor="")))

    guest = c.create_text(220, 414, text="游客进入（数据留在根目录）", fill="#7f8ea6",
                          font=(ui_font, 9))
    c.tag_bind(guest, "<Enter>", lambda _e: c.itemconfigure(guest, fill="#a9c4e6"))
    c.tag_bind(guest, "<Leave>", lambda _e: c.itemconfigure(guest, fill="#7f8ea6"))

    foot = c.create_text(220, 476,
                         text="密码只做本机校验、不上传；忘记密码可删 users.json 重置",
                         fill="#66707e", font=(ui_font, 9), width=356)

    # ---------- 行为 ----------
    def set_tip(text: str, color: str = RED) -> None:
        c.itemconfigure(tip, text=text, fill=color)

    def set_mode(mode: str) -> None:
        state["mode"] = mode
        draw_tabs()
        draw_button()
        set_tip("")
        entries["user"].focus_set()

    def do_login() -> None:
        u, p = entries["user"].get().strip(), entries["pw"].get()
        if not u:
            set_tip("请先填用户名")
            return
        if not exists(u):
            set_tip("没有这个用户（第一次用请点「注册」）")
            return
        if not verify(u, p):
            set_tip("密码不对")
            return
        result["user"] = u
        win.destroy()

    def do_register() -> None:
        err = register(entries["user"].get().strip(), entries["pw"].get())
        if err:
            set_tip(err)
            return
        set_tip("注册好了，正在进入…", GREEN)
        win.after(420, do_login)

    def submit() -> None:
        do_login() if state["mode"] == "login" else do_register()

    def _hit(x: int, y: int) -> str:
        if 128 <= y <= 168:
            if 52 <= x <= 218:
                return "tab:login"
            if 222 <= x <= 388:
                return "tab:register"
        if 336 <= y <= 384 and 48 <= x <= 392:
            return "submit"
        if 270 <= y <= 300 and 350 <= x <= 386:
            return "eye"
        if 400 <= y <= 430 and 130 <= x <= 310:
            return "guest"
        return ""

    def on_click(event) -> None:
        h = _hit(event.x, event.y)
        if h == "tab:login":
            set_mode("login")
        elif h == "tab:register":
            set_mode("register")
        elif h == "submit":
            submit()
        elif h == "eye":
            toggle_eye()
        elif h == "guest":
            do_guest()

    def on_motion(event) -> None:
        h = _hit(event.x, event.y)
        want = h == "submit"
        if want != btn.get("hot"):
            btn["hot"] = want
            draw_button()
        c.configure(cursor="hand2" if h else "")

    c.bind("<Button-1>", on_click)
    c.bind("<Motion>", on_motion)

    def do_guest() -> None:
        result["user"] = ""
        win.destroy()

    draw_tabs()
    draw_button()

    entries["user"].focus_set()
    win.bind("<Return>", lambda _e: submit())
    win.protocol("WM_DELETE_WINDOW", win.destroy)

    def _center_keep() -> None:
        win.geometry(f"{W}x{H}+{(sw - W) // 2}+{max(36, (sh - H) // 3)}")

    win.after(30, _center_keep)
    root.wait_window(win)
    return result["user"]
