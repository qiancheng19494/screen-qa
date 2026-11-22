"""函数图像辅助（纯 PIL，零依赖）。

用途：题目里出现 f(x) = ... 时，画一张坐标图（零点自动标出），
让人一眼看到「根在哪、有没有两个交点」，比纯文字更不容易看错。

安全：只接受白名单字符与函数名，先做隐式乘法替换，再用受限 eval
（不允许属性访问、下划线、导入等）。
"""
from __future__ import annotations

import math
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

FONT_PATHS = ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf")

# 允许的字符（先把 ^ 变成 **，再校验）
_ALLOWED_RE = re.compile(r"^[0-9xX+\-*/^()., a-z]+$")
_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "sqrt": math.sqrt, "abs": abs, "exp": math.exp,
    "log": math.log, "ln": math.log, "log10": math.log10,
    "pi": math.pi, "e": math.e,
}
_FUNC_RE = re.compile(r"f\s*\(\s*x\s*\)\s*=\s*([^\n\r;]+)", re.I)
_Y_RE = re.compile(r"(?:^|\n)\s*y\s*=\s*([^\n\r;]+)", re.I)


def extract_function(text: str) -> Optional[str]:
    """从题目文本里抓出 f(x) = ...（拿不到返回 None）。"""
    if not text:
        return None
    for rx in (_FUNC_RE, _Y_RE):
        m = rx.search(text)
        if not m:
            continue
        expr = m.group(1).strip()
        # 去掉后面可能跟着的中文说明/换行残留
        expr = re.split(r"[，。？?,]|  ", expr)[0].strip()
        expr = expr.replace("−", "-").replace("–", "-").replace("×", "*")
        expr = expr.replace("÷", "/").replace("²", "^2").replace("³", "^3")
        if expr and _ALLOWED_RE.match(expr.replace("**", "^")):
            return expr
    return None


def _to_python(expr: str) -> str:
    s = expr.replace("^", "**")
    # 隐式乘法：2x -> 2*x，x( -> x*(，)x -> )*x，)( 同理
    s = re.sub(r"(\d)\s*([a-zA-Z(])", r"\1*\2", s)
    s = re.sub(r"\)\s*([a-zA-Z0-9(])", r")*\1", s)
    s = re.sub(r"([a-zA-Z])\s*\(", r"\1*(", s)
    # 但函数名后面的括号不能变成 *（sin( -> sin*(）
    for fn in ("sin", "cos", "tan", "sqrt", "log", "ln", "abs", "exp", "log10"):
        s = s.replace(f"{fn}*(", f"{fn}(")
    return s


def _compiled(expr: str):
    py = _to_python(expr)
    code = compile(py, "<plot>", "eval")

    def f(x: float) -> float:
        env = dict(_FUNCS)
        env["x"] = x
        return float(eval(code, {"__builtins__": {}}, env))  # noqa: S307

    return f


def _nice_step(span: float) -> float:
    raw = span / 8.0
    for step in (0.5, 1, 2, 5, 10, 20, 50):
        if raw <= step:
            return float(step)
    return 100.0


def plot_function(expr: str, width: int = 560, height: int = 400) -> Optional[Image.Image]:
    """把 y = f(x) 画成图，返回 PIL.Image（失败返回 None）。"""
    try:
        f = _compiled(expr)
    except Exception:  # noqa: BLE001
        return None

    xmin, xmax = -10.0, 10.0
    samples = 800
    pts: list[tuple[float, float]] = []
    for i in range(samples + 1):
        x = xmin + (xmax - xmin) * i / samples
        try:
            y = f(x)
        except Exception:  # noqa: BLE001
            y = None
        if y is None or not math.isfinite(y) or abs(y) > 1e6:
            pts.append((x, float("nan")))
        else:
            pts.append((x, y))

    ys = [y for _x, y in pts if not math.isnan(y)]
    if not ys:
        return None
    lo, hi = min(ys), max(ys)
    pad = max(1.0, (hi - lo) * 0.15)
    ymin, ymax = lo - pad, hi + pad
    if hi - lo < 1e-9:
        ymin, ymax = lo - 1, hi + 1
    # 控制极端值，避免曲线把坐标轴压扁
    if ymax - ymin > 400:
        ymin, ymax = max(ymin, -200), min(ymax, 200)

    img = Image.new("RGB", (width, height), "#0f1319")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT_PATHS[0], 13)
        font_b = ImageFont.truetype(FONT_PATHS[0], 14)
    except Exception:  # noqa: BLE001
        font = font_b = ImageFont.load_default()

    left, right, top, bottom = 46, width - 18, 16, height - 34
    pw, ph = right - left, bottom - top

    def sx(x: float) -> float:
        return left + (x - xmin) / (xmax - xmin) * pw

    def sy(y: float) -> float:
        return bottom - (y - ymin) / (ymax - ymin) * ph

    # 网格
    step_x = _nice_step(xmax - xmin)
    x = math.ceil(xmin / step_x) * step_x
    while x <= xmax + 1e-9:
        X = sx(x)
        d.line([(X, top), (X, bottom)], fill="#1c2431")
        d.text((X - 8, bottom + 6), f"{x:g}", font=font, fill="#7c8798")
        x += step_x
    step_y = _nice_step(ymax - ymin)
    y = math.ceil(ymin / step_y) * step_y
    while y <= ymax + 1e-9:
        Y = sy(y)
        d.line([(left, Y), (right, Y)], fill="#1c2431")
        d.text((left - 40, Y - 7), f"{y:g}", font=font, fill="#7c8798")
        y += step_y

    # 坐标轴
    if ymin <= 0 <= ymax:
        d.line([(left, sy(0)), (right, sy(0))], fill="#46536b")
    if xmin <= 0 <= xmax:
        d.line([(sx(0), top), (sx(0), bottom)], fill="#46536b")

    # 曲线
    prev = None
    for x, y in pts:
        if math.isnan(y) or y < ymin - (ymax - ymin) or y > ymax + (ymax - ymin):
            prev = None
            continue
        cur = (sx(x), sy(max(min(y, ymax), ymin)))
        if prev:
            d.line([prev, cur], fill="#4d9dff", width=2)
        prev = cur

    # 零点（相邻采样符号变化 → 二分细化）
    roots: list[float] = []
    clean = [(x, y) for x, y in pts if not math.isnan(y)]
    for (x1, y1), (x2, y2) in zip(clean, clean[1:]):
        if y1 == 0:
            roots.append(x1)
            continue
        if y1 * y2 < 0:
            a, b = x1, x2
            for _ in range(40):
                m = (a + b) / 2
                try:
                    fm = f(m)
                except Exception:  # noqa: BLE001
                    break
                if not math.isfinite(fm):
                    break
                if (f(a) * fm) <= 0:
                    b = m
                else:
                    a = m
            roots.append(round((a + b) / 2, 3))
    seen: set[float] = set()
    uniq = []
    for r in roots:
        if all(abs(r - s) > 1e-3 for s in seen):
            seen.add(r)
            uniq.append(r)

    for r in uniq[:6]:
        if xmin <= r <= xmax:
            X, Y = sx(r), sy(0)
            d.ellipse([X - 4, Y - 4, X + 4, Y + 4], fill="#3ddc84")
            label = f"x={r:g}"
            tx = min(max(X + 6, left + 2), right - 46)
            ty = max(Y - 20, top + 2)
            d.text((tx, ty), label, font=font_b, fill="#3ddc84")

    pretty = expr.replace("^2", "²").replace("^3", "³").replace("*", "·")
    title = f"y = {pretty}"
    d.text((left, 2), title[:56], font=font_b, fill="#c8d0dd")
    if uniq:
        d.text((right - 4, 2), "零点：" + "，".join(f"{r:g}" for r in uniq[:4]),
               font=font, fill="#3ddc84", anchor="ra")
    return img

def extract_functions(text: str) -> list[str]:
    """把题面里所有 f(x)=... / y=... 都抠出来（用于画多条曲线）。"""
    out = []
    for m in re.finditer(r"(?:f\s*\(\s*x\s*\)|[yY])\s*=\s*([^;\n,，。]{1,60})", text or ""):
        cand = m.group(1).strip().rstrip(".")
        # "y = x^2 - 1 and y = 2x + 1" 这种会被一次吃掉：切掉 and / &
        for part in re.split(r"\s+and\s+|\s*&\s*", cand):
            part = part.strip().rstrip(".")
            part = re.sub(r"^[yYfF]\s*\(?\s*x?\s*\)?\s*=\s*", "", part).strip()
            if part and part not in out:
                out.append(part)
        if len(out) >= 3:
            break
    return out[:3]


_CURVE_COLORS = ["#4d9dff", "#ffc857", "#3ddc84"]


def plot_multi(exprs, width: int = 560, height: int = 400):
    """把一条或多条 y=f(x) 画在同一张图上，并标零点 / 曲线交点。"""
    if isinstance(exprs, str):
        exprs = [exprs]
    exprs = [e for e in list(exprs)[:3] if e and str(e).strip()]
    if not exprs:
        return None
    funcs = []
    for e in exprs:
        try:
            funcs.append(_compiled(e))
        except Exception:  # noqa: BLE001
            return None

    xmin, xmax = -10.0, 10.0
    samples = 800
    series = []
    for f in funcs:
        pts = []
        for i in range(samples + 1):
            x = xmin + (xmax - xmin) * i / samples
            try:
                y = f(x)
            except Exception:  # noqa: BLE001
                y = None
            if y is None or not math.isfinite(y) or abs(y) > 1e6:
                pts.append((x, float("nan")))
            else:
                pts.append((x, y))
        series.append(pts)

    ys = [y for pts in series for _x, y in pts if not math.isnan(y)]
    if not ys:
        return None
    lo, hi = min(ys), max(ys)
    pad = max(1.0, (hi - lo) * 0.15)
    ymin, ymax = lo - pad, hi + pad
    if hi - lo < 1e-9:
        ymin, ymax = lo - 1, hi + 1
    if ymax - ymin > 400:
        ymin, ymax = max(ymin, -200), min(ymax, 200)

    img = Image.new("RGB", (width, height), "#0f1319")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT_PATHS[0], 13)
        font_b = ImageFont.truetype(FONT_PATHS[0], 14)
    except Exception:  # noqa: BLE001
        font = font_b = ImageFont.load_default()

    left, right, top, bottom = 46, width - 18, 16, height - 34
    pw, ph = right - left, bottom - top

    def sx(x: float) -> float:
        return left + (x - xmin) / (xmax - xmin) * pw

    def sy(y: float) -> float:
        return bottom - (y - ymin) / (ymax - ymin) * ph

    step_x = _nice_step(xmax - xmin)
    x = math.ceil(xmin / step_x) * step_x
    while x <= xmax + 1e-9:
        X = sx(x)
        d.line([(X, top), (X, bottom)], fill="#1c2431")
        d.text((X - 8, bottom + 6), f"{x:g}", font=font, fill="#7c8798")
        x += step_x
    step_y = _nice_step(ymax - ymin)
    y = math.ceil(ymin / step_y) * step_y
    while y <= ymax + 1e-9:
        Y = sy(y)
        d.line([(left, Y), (right, Y)], fill="#1c2431")
        d.text((left - 40, Y - 7), f"{y:g}", font=font, fill="#7c8798")
        y += step_y

    if ymin <= 0 <= ymax:
        d.line([(left, sy(0)), (right, sy(0))], fill="#46536b")
    if xmin <= 0 <= xmax:
        d.line([(sx(0), top), (sx(0), bottom)], fill="#46536b")

    for idx, pts in enumerate(series):
        color = _CURVE_COLORS[idx % len(_CURVE_COLORS)]
        prev = None
        for x, y in pts:
            if math.isnan(y) or y < ymin - (ymax - ymin) or y > ymax + (ymax - ymin):
                prev = None
                continue
            cur = (sx(x), sy(max(min(y, ymax), ymin)))
            if prev:
                d.line([prev, cur], fill=color, width=2)
            prev = cur

    def _bisect(g, a: float, b: float) -> float:
        for _ in range(40):
            m = (a + b) / 2
            try:
                if g(a) * g(m) <= 0:
                    b = m
                else:
                    a = m
            except Exception:  # noqa: BLE001
                break
        return round((a + b) / 2, 3)

    marks = []
    for idx, f in enumerate(funcs):
        clean = [(x, y) for x, y in series[idx] if not math.isnan(y)]
        for (x1, y1), (x2, y2) in zip(clean, clean[1:]):
            if y1 == 0:
                marks.append((x1, 0.0, "x=%g" % x1))
            elif y1 * y2 < 0:
                r = _bisect(f, x1, x2)
                marks.append((r, 0.0, "x=%g" % r))
    if len(funcs) == 2:
        f0, f1 = funcs
        g = lambda t: f0(t) - f1(t)  # noqa: E731
        clean = []
        for i in range(samples + 1):
            t = xmin + (xmax - xmin) * i / samples
            try:
                clean.append((t, g(t)))
            except Exception:  # noqa: BLE001
                clean.append((t, float("nan")))
        clean = [p for p in clean if math.isfinite(p[1])]
        for (x1, y1), (x2, y2) in zip(clean, clean[1:]):
            if y1 * y2 < 0:
                r = _bisect(g, x1, x2)
                try:
                    yy = f0(r)
                except Exception:  # noqa: BLE001
                    continue
                marks.append((r, yy, "cross x=%g" % r))

    seen = set()
    shown = 0
    for r, yy, label in marks:
        if shown >= 6 or not (xmin <= r <= xmax):
            continue
        if any(abs(r - s) < 1e-3 for s in seen):
            continue
        seen.add(r)
        X, Y = sx(r), sy(max(min(yy, ymax), ymin))
        d.ellipse([X - 4, Y - 4, X + 4, Y + 4], fill="#3ddc84")
        ty = max(Y - 20 - (shown % 3) * 16, top + 2)
        if r < 0:
            d.text((max(X - 6, left + 2), ty), label, font=font_b, fill="#3ddc84", anchor="ra")
        else:
            d.text((min(X + 6, right - 60), ty), label, font=font_b, fill="#3ddc84")
        shown += 1

    for idx in range(len(funcs)):
        color = _CURVE_COLORS[idx % len(_CURVE_COLORS)]
        d.line([(left + 4, top + 12 + idx * 16), (left + 24, top + 12 + idx * 16)], fill=color, width=3)
        d.text((left + 30, top + 4 + idx * 16), "y" + str(idx + 1), font=font, fill="#aab4c4")

    return img


def plot_function(expr: str, width: int = 560, height: int = 400):
    """兼容旧调用：等价于画一条曲线。"""
    return plot_multi([expr], width, height)
