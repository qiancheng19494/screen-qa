"""OCR 数字/符号核对：给"题面可能被认错"提供便宜的旁证。

不做 OCR 纠错（那是 ocrfix 的事），只回答一个问题：
"答案里的数字，在模型自己的推理里出现过吗？" 没出现过就提示人工核对。
"""

from __future__ import annotations

import re

import verify

__all__ = ["audit"]

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
