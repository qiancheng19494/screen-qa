"""OCR 文本修正层：把 Windows OCR 在数学/公式上的高频误识别改回来。

原则：只改**认定明确**的东西，能用上下文限定的就限定；改不动的不猜。
目的是让喂给模型的文字更接近题目原样（模型本身也会纠错，但不能全靠它）。
"""
from __future__ import annotations

import re

# 1) 一对一替换：CJK 形近字 / 全角数学符号
_CHAR_MAP = {
    "氵去": "法", "亻介": "价", "贝刂": "则", "氵则": "则",
    "１": "1", "２": "2", "３": "3", "４": "4", "５": "5",
    "６": "6", "７": "7", "８": "8", "９": "9", "０": "0",
    "Ｘ": "x", "ｘ": "x", "Ｙ": "y", "ｙ": "y",
    "（": "(", "）": ")", "［": "[", "］": "]",
    "／": "/", "＝": "=", "＋": "+", "＊": "*", "＾": "^",
    "％": "%", "＜": "<", "＞": ">",
}

# 2) 多字修正（顺序重要：先长后短）
_PAIR_MAP = [
    (re.compile(r"[阝卩]\s*in"), "sin"),
    (re.compile(r"阝\s*sec"), "sec"),
    (re.compile(r"c\s*〇\s*s"), "cos"),
    (re.compile(r"l\s*n\b"), "ln"),
    (re.compile(r"log\s*1\s*0"), "log10"),
]

# 3) 减号：OCR 常把 - 认成中文"一"/破折号/全角横线，只在数字或字母旁边才改
_MINUS = re.compile(r"(?<=[0-9])\s*[一—–−－]\s*(?=[0-9])")

# 4) 上标压平（3X2）**不猜**：可能是 3x² 也可能是 3×2，猜错比不猜更糟

# 5) 竖线/方括号被识别成 ] 或 ) 的公式收尾，不动

# 6) 箭头："→" 常被认成 "—O" / "一O"
_ARROW = re.compile(r"([xX])\s*[—–一]\s*[Oo0]\s*\)")
_ARROW2 = re.compile(r"→|一>|—>")


def _tilde(s: str) -> str:
    """RapidOCR 把上标 ^ 认成 ~（实测 (x~2-9)）→ 补回幂运算。"""
    import re as _re
    return _re.sub(r"(?<=[0-9A-Za-z)])\s*~\s*(?=[0-9A-Za-z(])", "^", s)


def repair(text: str) -> tuple[str, int]:
    """返回 (修正后的文本, 改动处数)。"""
    if not text:
        return text or "", 0
    out = _tilde(text)
    n = 0

    for bad, good in _CHAR_MAP.items():
        c = out.count(bad)
        if c:
            out = out.replace(bad, good)
            n += c

    for pat, good in _PAIR_MAP:
        out, k = pat.subn(good, out)
        n += k

    # 先修箭头（x—O) → x→0)），否则下一步的减号规则会把 — 变成 -
    out, k = _ARROW.subn(r"\1→0)", out)
    n += k
    out, k = _ARROW2.subn("→", out)
    n += k

    out, k = _MINUS.subn("-", out)
    n += k

    # 压缩"每个汉字之间被塞空格"导致的怪异空白（保留换行）
    lines = []
    for ln in out.splitlines():
        lines.append(re.sub(r"[ \t]{2,}", " ", ln).rstrip())
    out = "\n".join(lines)
    return out, n
