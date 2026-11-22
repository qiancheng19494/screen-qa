"""OCR 数字/符号核对：给"题面可能被认错"提供便宜的旁证。

不做 OCR 纠错（那是 ocrfix 的事），只回答一个问题：
"答案里的数字，在模型自己的推理里出现过吗？" 没出现过就提示人工核对。
"""

from __future__ import annotations

import re

import verify

__all__ = ["audit", "risk"]

_EMPTY_PAREN = re.compile(r"\(\s*\)")

_EQ_SUPERSCRIPT = re.compile(r"\d[xX]\d(?!\d)|[xX]\s?2(?!\d)|[xX]\s?3(?!\d)")


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def audit(question: str, answer: str, fill: str = "", reasoning: str = "") -> list[str]:
    warns: list[str] = []
    q = question or ""
    try:
        if _EQ_SUPERSCRIPT.search(q):
            warns.append("题目里像有被 OCR 压平的上标（如 3X2 可能是 3x²），模型已按题意自行纠正，"
                         "建议看一眼原图确认指数")
    except Exception:  # noqa: BLE001
        pass
    try:
        # 整行/系数被吞：sqrt() 空括号、括号里的式子全空 —— 实测 Windows OCR 常见
        if re.search(r"sqrt\s*\(\s*\)", q, re.I) or re.search(r"\(\s*\)", q):
            warns.append("OCR 读到了空括号（如 sqrt()x），说明括号里的系数被吞了；"
                         "建议把上一行题干一起框进来，或按 Ctrl+Alt+T 直接用剪贴板文字作答")
        elif len(q) >= 20 and not re.search(r"[0-9=^/<>]", q):
            warns.append("题面里一个数字/符号都没读到，很可能有整行纯符号表达式被 OCR 丢了；"
                         "建议框大一点（带上题干行），或按 Ctrl+Alt+T 用文字作答")
    except Exception:  # noqa: BLE001
        pass
    try:
        want = verify.extract_numbers(fill or answer)
        have = verify.extract_numbers(reasoning)
        if want and have:
            missing = [n for n in want if not any(_close(n, h) for h in have)]
            if missing:
                shown = "、".join(("{0:g}".format(n)) for n in missing[:3])
                warns.append("答案里的数值 {0} 没出现在推理里，建议核对题面数字是否被看错".format(shown))
    except Exception:  # noqa: BLE001
        pass
    return warns

def risk(question: str) -> str:
    """返回 OCR 不可靠的具体原因；可靠则返回空串（供自动改用剪贴板文字）。"""
    q = question or ""
    try:
        if _EMPTY_PAREN.search(q) or re.search(r"sqrt\s*\(\s*\)|\(\s*\)", q, re.I):
            return "读到了空括号（系数可能被吞）"
        if len(q) >= 20 and not re.search(r"[0-9=^/<>]", q):
            return "题面里没有数字/符号（可能有整行被丢）"
        if re.search(r"\bsqrt\b", q, re.I) and not re.search(r"sqrt\s*\(\s*[0-9x]", q, re.I):
            return "根号内容不完整"
    except Exception:  # noqa: BLE001
        return ""
    return ""
