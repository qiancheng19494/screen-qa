"""再生成 20 道回归用例（补不等式/指数对数/应用题/绝对值），追加进 tests/cases.json。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "cases"
OUT.mkdir(exist_ok=True)
FONT = "C:/Windows/Fonts/segoeui.ttf"
FONT_B = "C:/Windows/Fonts/seguisb.ttf"

CASES = [
    ("c31_ineq_mul", ["Solve the inequality.", "5x - 3 > 12"], ["5"], "value", "一元一次不等式"),
    ("c32_ineq_neg", ["Solve the inequality.", "-4x + 2 <= 14"], ["-3"], "value", "负系数不等式"),
    ("c33_paren", ["Solve for x.", "5(x - 2) = 3x + 4"], ["7"], "value", "带括号一次方程"),
    ("c34_square", ["Solve the equation.", "x^2 = 49", "Enter both answers, smaller first."], ["-7", "7"], "set", "平方根两解"),
    ("c35_quad2", ["Solve the equation.", "2x^2 - 8 = 0"], ["-2", "2"], "set", "二次方程"),
    ("c36_log8", ["Evaluate the logarithm.", "log_2(8)"], ["3"], "value", "对数求值"),
    ("c37_pow5", ["Evaluate the power.", "2^5"], ["32"], "value", "指数求值"),
    ("c38_slope2", ["Find the slope of the line through", "(-2, 1) and (2, 9)."], ["2"], "value", "斜率-负坐标"),
    ("c39_percent65", ["What is 40% of 65?"], ["26"], "value", "百分比"),
    ("c40_prop", ["Solve the proportion.", "3/4 = x/20"], ["15"], "value", "比例"),
    ("c41_avg", ["Find the average of 12, 18 and 24."], ["18"], "value", "平均数"),
    ("c42_interest", ["Find the simple interest.", "$500 at 6% for 2 years"], ["60"], "value", "单利"),
    ("c43_dist", ["A truck drives 55 miles per hour", "for 4 hours. How far does it travel?"], ["220"], "value", "路程应用题"),
    ("c44_fvalue2", ["For f(x) = 2x^2 - 3x + 1,", "find f(3)."], ["10"], "value", "二次函数值"),
    ("c45_abs", ["Solve the equation.", "|x - 3| = 5", "Enter both answers, smaller first."], ["-2", "8"], "set", "绝对值方程"),
    ("c46_halfx", ["Solve for x.", "1/2 x + 3 = 7"], ["8"], "value", "分数系数"),
    ("c47_eval_line", ["For y = -2x + 7,", "find y when x = 4."], ["-1"], "value", "一次函数代入"),
    ("c48_exp16", ["Solve for x.", "2^x = 16"], ["4"], "value", "指数方程"),
    ("c49_percent2", ["What is 25% of 96?"], ["24"], "value", "百分比"),
    ("c50_sqrtx", ["Solve for x.", "sqrt(x) = 9"], ["81"], "value", "平方根方程"),
]


def draw(name: str, lines: list[str]) -> Path:
    w, h = 940, 120 + 46 * len(lines) + 150
    img = Image.new("RGB", (w, h), "#ffffff")
    d = ImageDraw.Draw(img)
    fb = ImageFont.truetype(FONT_B, 26)
    f = ImageFont.truetype(FONT, 26)
    d.text((34, 30), "Question", font=fb, fill="#1b1b1f")
    y = 84
    for i, line in enumerate(lines):
        d.text((34, y), line, font=(fb if i == 0 else f), fill="#1b1b1f")
        y += 46
    box_y = y + 26
    d.rectangle([34, box_y, 34 + 420, box_y + 46], outline="#8a8a92", width=2)
    d.text((44, box_y + 10), "Enter your answer here",
           font=ImageFont.truetype(FONT, 19), fill="#9a9aa2")
    p = OUT / f"{name}.png"
    img.save(p)
    return p


def main() -> None:
    new = []
    for name, lines, expect, mode, note in CASES:
        p = draw(name, lines)
        item = {"image": f"cases/{p.name}", "expect": expect, "mode": mode,
                "note": note}
        new.append(item)
        print("drew", p.name)
    cases_path = ROOT / "tests" / "cases.json"
    data = json.loads(cases_path.read_text(encoding="utf-8")) if cases_path.exists() else []
    have = {c.get("image") for c in data}
    added = [c for c in new if c["image"] not in have]
    data.extend(added)
    cases_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"total cases: {len(data)} (+{len(added)})")


if __name__ == "__main__":
    sys.exit(main())
