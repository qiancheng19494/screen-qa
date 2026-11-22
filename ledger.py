"""账本：把 records.jsonl 汇总成"今日 / 累计 / 按模型 / 30 天柱状图"。

灵感来自朋友项目 SpellCardHomework 的"香火钱账本"（tokens、耗时、费用、30 天图表），
这里按我们的记录格式重写：数据来自 config.load_records()，绘图用 PIL，不引入依赖。
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]

BG = "#0f1319"
CARD = "#151b24"
GRID = "#1f2833"
FG = "#e9ecf2"
DIM = "#8b95a5"
ACCENT = "#4d9dff"
GREEN = "#3ddc84"


def _font(size: int, bold: bool = False):
    for path in FONT_CANDIDATES[1 if bold else 0:] + FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def summarize(records: list[dict]) -> dict:
    today = time.strftime("%Y-%m-%d")
    tot_cost = 0.0
    today_cost = 0.0
    today_n = 0
    by_model: dict[str, dict] = defaultdict(lambda: {"n": 0, "cost": 0.0, "tok": 0})
    by_day: dict[str, dict] = defaultdict(lambda: {"n": 0, "cost": 0.0})
    for r in records:
        cost = float(r.get("cost") or 0.0)
        tok = int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        model = str(r.get("model") or "?")
        tot_cost += cost
        m = by_model[model]
        m["n"] += 1
        m["cost"] += cost
        m["tok"] += tok
        ts = str(r.get("ts") or "")
        day = ts[:10] if len(ts) >= 10 else ""
        if day:
            by_day[day]["n"] += 1
            by_day[day]["cost"] += cost
        if day == today:
            today_n += 1
            today_cost += cost
    return {
        "total_n": len(records),
        "total_cost": tot_cost,
        "today_n": today_n,
        "today_cost": today_cost,
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["n"])),
        "by_day": dict(by_day),
    }


def draw_chart(records: list[dict], days: int = 30, width: int = 620,
               height: int = 200) -> Image.Image | None:
    stat = summarize(records)
    by_day = stat["by_day"]
    today = time.localtime()
    stamps = []
    for i in range(days - 1, -1, -1):
        t = time.localtime(time.time() - i * 86400)
        stamps.append(time.strftime("%Y-%m-%d", t))
    counts = [by_day.get(s, {}).get("n", 0) for s in stamps]
    if not any(counts):
        return None

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_small = _font(11)
    f_bold = _font(13, True)

    left, right, top, bottom = 34, width - 14, 26, height - 26
    pw, ph = right - left, bottom - top
    peak = max(counts) or 1

    d.text((12, 6), f"近 {days} 天作答次数", font=f_bold, fill=FG)
    d.text((width - 12, 7), f"合计 {stat['total_n']} 题 · ¥{stat['total_cost']:.4f}",
           font=f_small, fill=DIM, anchor="ra")

    # 网格
    for i in range(4):
        y = top + ph * i / 3
        d.line([(left, y), (right, y)], fill=GRID)
    # 柱
    n = len(counts)
    slot = pw / n
    bar_w = max(3, int(slot * 0.62))
    for i, c in enumerate(counts):
        if not c:
            continue
        h = int(ph * (c / peak))
        x = left + slot * i + (slot - bar_w) / 2
        y2 = bottom - h
        color = ACCENT if i < n - 1 else GREEN
        d.rounded_rectangle([x, y2, x + bar_w, bottom], radius=2, fill=color)

    # 横轴：首/中/尾
    for idx, s in ((0, stamps[0]), (n // 2, stamps[n // 2]), (n - 1, stamps[-1])):
        x = left + slot * idx + slot / 2
        d.text((x, bottom + 6), s[5:], font=f_small, fill=DIM, anchor="ma")

    d.text((right, top + 2), f"峰值 {peak} 题/天", font=f_small, fill=DIM, anchor="ra")
    return img
