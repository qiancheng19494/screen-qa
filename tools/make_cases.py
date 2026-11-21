"""生成 22 道新回归用例的题图（MyMathLab 风格：题干 + 空作答框）。

只依赖 Pillow；文字用 Segoe UI（能渲染 √ > ≤ 等符号）。
生成后把用例清单写到 tests/cases_new.json，再和原有 8 条合并成 tests/cases.json。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "cases"
FONT = "C:/Windows/Fonts/segoeui.ttf"
FONT_B = "C:/Windows/Fonts/seguisb.ttf"

CASES = [
    # name, lines, expect, mode, note
    ("c01_frac", ["Solve for x.", "3/4 x = 9"], ["12"], "value", "分数系数"),
    ("c02_radical", ["Solve for x.", "sqrt(2x + 1) = 5"], ["12"], "value", "根式方程"),
    ("c03_radical2", ["Solve for x.", "sqrt(x) = 7"], ["49"], "value", "平方根"),
    ("c04_ineq", ["Solve the inequality.", "-3x + 6 > 15"], ["x<-3", "x<-3"], "text", "不等式方向翻转"),
    ("c05_exp", ["Solve for x.", "2^x = 32"], ["5"], "value", "指数方程"),
    ("c06_log", ["Solve for x.", "log_2(x) = 3"], ["8"], "value", "对数方程"),
    ("c07_quad", ["Solve the equation.", "2x^2 - 8x + 6 = 0", "Enter both answers, smaller first."], ["1", "3"], "set", "二次方程两解"),
    ("c08_percent", ["What is 15% of 240?"], ["36"], "value", "百分比"),
    ("c09_fracadd", ["Add and simplify.", "1/2 + 1/3"], ["5/6"], "text", "分数相加"),
    ("c10_simplify", ["Simplify the expression.", "(x^2 - 9) / (x - 3)"], ["x+3"], "text", "化简"),
    ("c11_slope", ["Find the slope of the line through", "(1, 2) and (3, 8)."], ["3"], "value", "斜率"),
    ("c12_fvalue", ["For f(x) = x^2 + 2x - 3,", "find f(2)."], ["5"], "value", "函数值"),
    ("c13_prop", ["Solve the proportion.", "3/5 = x/20"], ["12"], "value", "比例"),
    ("c14_discount", ["A shirt costs $40 after a 20% discount.", "What was the original price?"], ["50"], "value", "应用题-折扣"),
    ("c15_speed", ["A train travels at 60 miles per hour", "for 2.5 hours. How far does it go?"], ["150"], "value", "应用题-路程"),
    ("c16_average", ["Find the average of 12, 18, 24 and 30."], ["21"], "value", "平均数"),
    ("c17_increase", ["A price rises from $80 to $100.", "What is the percent increase?"], ["25"], "value", "增长率"),
    ("c18_comb", ["Evaluate the combination.", "C(5, 2)"], ["10"], "value", "组合数"),
    ("c19_system", ["Solve the system.", "2x + y = 7", "x - y = 2"], ["3", "4"], "set", "方程组"),
    ("c20_decimal", ["Solve for x.", "0.25x = 6"], ["24"], "value", "小数系数"),
    ("c21_negative", ["Solve for x.", "-2(x - 4) = 10"], ["-1"], "value", "负数解"),
    ("c22_fraction_mix", ["Solve for x.", "x/3 + 2 = 7"], ["15"], "value", "分式方程"),
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
    d.text((44, box_y + 10), "Enter your answer here", font=ImageFont.truetype(FONT, 19), fill="#9a9aa2")
    path = OUT / f"{name}.png"
    img.save(path)
    return path


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    OUT.mkdir(exist_ok=True)
    rows = []
    for name, lines, expect, mode, note in CASES:
        path = draw(name, lines)
        rows.append({"image": f"cases/{path.name}", "expect": expect,
                     "mode": mode, "note": note})
    (ROOT / "tests" / "cases_new.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(rows)} 张题图 → cases/")
    print("清单 → tests/cases_new.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
