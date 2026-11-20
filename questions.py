"""把一屏多题的文字切成一道一道。

为什么放在 OCR 之后、模型之前：
  * 切开后每道题都走原来那套「多次采样 + 投票 + 审核」，准确度和单题完全一样；
  * 不用让模型自己分题（模型分题会串题，还会把答案张冠李戴）。

切分规则（保守，宁可切不出来也不乱切）：
  1. 行首编号：`1.` `2)` `(3)` `4、` 这类；编号必须递增或至少不重复，否则不切；
  2. 每个片段要有点内容（去空白后 ≥ 12 字），太短的碎片并回上一题；
  3. 选项行（`A.` `B)` …）永远归属当前题，不会被当成新题；
  4. 只切出 1 段就返回空列表（让上层按单题处理）。
"""
from __future__ import annotations

import re

_NUM_RE = re.compile(
    r"^\s*(?:第\s*)?[（(]?\s*(\d{1,2})\s*"
    r"(?:[.．。、,，)）:：])\s*(?!\d)(?=\S)"
)
_OPT_RE = re.compile(r"^\s*[（(]?[A-Ha-h]\s*[.、)）]\s*\S")
_MIN_BODY = 12


def _is_option(line: str) -> bool:
    return bool(_OPT_RE.match(line))


def split_questions(text: str) -> list[str]:
    """返回切好的题目列表；切不出来返回 []。"""
    if not text or len(text.strip()) < 40:
        return []

    lines = [ln.rstrip() for ln in text.splitlines()]
    marks: list[tuple[int, int]] = []          # (行号, 编号)
    for i, ln in enumerate(lines):
        m = _NUM_RE.match(ln)
        if not m:
            continue
        if _is_option(ln):
            continue
        if int(m.group(1)) > 50:      # 页码、年份之类
            continue
        num = int(m.group(1))
        if marks and num <= marks[-1][1]:
            # 编号没递增（目录、年份、页码等）→ 不认为是题号
            continue
        marks.append((i, num))

    if len(marks) < 2:
        return []

    # 题号之前的文字（比如 "Homework 5" 标题）当作前言，丢弃
    chunks: list[str] = []
    for idx, (start, _num) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        body = "\n".join(ln for ln in lines[start:end] if ln.strip())
        chunks.append(body.strip())

    # 太短的碎片并到上一题（常见于 OCR 把一行拆成两行）
    merged: list[str] = []
    for c in chunks:
        if merged and len(re.sub(r"\s", "", c)) < _MIN_BODY:
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)

    merged = [c for c in merged if len(re.sub(r"\s", "", c)) >= _MIN_BODY]
    return merged if len(merged) >= 2 else []


_QNO_RE = re.compile(r"(?:Question|QUESTION|Question\s*#|Q)\s*(\d{1,3})\s*(?:of|/|／)\s*(\d(?:\s*\d){0,2})")
_QNO_ZH = re.compile(r"第\s*(\d{1,3})\s*题\s*(?:[/／]\s*(\d{1,3}))?")


def question_number(text: str):
    """从 OCR 文字里认出「第几题 / 共几题」（MyMathLab 顶部会写 Question 7 of 12）。

    返回 (当前, 总数) —— 总数认不出就给 None；完全没有题号返回 None。
    用途：连续模式靠题号判断"换题了没有"，比纯文字相似度准。
    """
    if not text:
        return None
    m = _QNO_RE.search(text)
    if m:
        cur = int(m.group(1))
        total = int(re.sub(r"\s+", "", m.group(2)))
        # OCR 常把 "12" 断成 "1 2"，那样会得到 total < cur，此时总数不可信
        return cur, (total if total > cur else None)
    m = _QNO_ZH.search(text)
    if m:
        cur = int(m.group(1))
        total = int(m.group(2)) if m.group(2) else None
        return cur, (total if total and total > cur else None)
    return None

# ---------------------------------------------------------------------------
# 答案格式要求（Pearson 经常卡"算对了但格式不对"：几位小数、最简分数、百分数…）
# ---------------------------------------------------------------------------
_NUM_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6}
_PLACE = {"tenth": 1, "hundredth": 2, "thousandth": 3,
          "integer": 0, "whole number": 0, "cent": 2}


def format_hint(text: str) -> dict:
    """从题目文字里抠出"答案要什么格式"。"""
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t)
    hint = {"decimals": None, "fraction": False, "percent": False,
            "simplest": False, "order": None, "comma": None}

    m = re.search(r"round(?:ed)?\s+to\s+(?:the\s+nearest\s+)?(" + "|".join(_PLACE) + r")", t)
    if m:
        hint["decimals"] = _PLACE[m.group(1)]
    if hint["decimals"] is None:
        m = (re.search(r"(\d)\s*decimal\s*places?", t)
             or re.search(r"(?:round(?:ed)?|answer)[^.]{0,24}?to\s+(?:the\s+nearest\s+)?"
                          r"(\w+)\s+decimal", t)
             or re.search(r"(\w+)\s+decimal\s+places?", t))
        if m:
            g = m.group(1).lower()
            hint["decimals"] = _NUM_WORD.get(g, 2)
    if re.search(r"\bsimplest\s+form|\bsimplify\b", t):
        hint["simplest"] = True
    if re.search(r"\bfraction\b|型如\s*a/b", t):
        hint["fraction"] = True
    if re.search(r"\bpercent(age)?\b|百分数", t):
        hint["percent"] = True
    if re.search(r"\bsmaller\s+first|\bfrom\s+smallest\s+to\s+largest", t):
        hint["order"] = "asc"
    if re.search(r"\blarger\s+first|\bfrom\s+largest\s+to\s+smallest", t):
        hint["order"] = "desc"
    if re.search(r"comma[- ]separated|separate[d]?\s+(?:your\s+answers?\s+)?(?:by|with)\s+(?:a\s+)?commas?", t):
        hint["comma"] = True
    return hint


def hint_text(hint: dict) -> str:
    """把 hint 说成一句人能读懂的要求，塞进提示词。"""
    parts = []
    d = hint.get("decimals")
    if d == 0:
        parts.append("四舍五入到整数")
    elif isinstance(d, int):
        parts.append(f"保留 {d} 位小数")
    if hint.get("simplest"):
        parts.append("分数化成最简形式")
    if hint.get("fraction"):
        parts.append("写成分数（如 3/4，不要写成小数）")
    if hint.get("percent"):
        parts.append("百分数形式（如 25%）")
    if hint.get("order") == "asc":
        parts.append("多个答案按从小到大排列")
    elif hint.get("order") == "desc":
        parts.append("多个答案按从大到小排列")
    if hint.get("comma"):
        parts.append("多个答案用逗号分隔")
    return "；".join(parts)


def _num(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def apply_format(fill: str, hint: dict) -> str:
    """按格式要求清洗/改写入框值：四舍五入、分数最简、百分数、排序。"""
    if not fill or not hint:
        return fill
    out = fill

    # 多值：排序 + 逗号
    if "," in out or "，" in out:
        vals = [v.strip() for v in re.split(r"[,，]", out) if v.strip()]
        nums = [_num(v) for v in vals]
        if hint.get("order") and all(n is not None for n in nums):
            pairs = sorted(zip(nums, vals), reverse=(hint["order"] == "desc"))
            out = ", ".join(v for _, v in pairs)
        elif hint.get("comma"):
            out = ", ".join(vals)

    # 分数最简
    m = re.fullmatch(r"(-?\d+)\s*/\s*(\d+)", out.strip())
    if m and hint.get("simplest"):
        from math import gcd
        a, b = int(m.group(1)), int(m.group(2))
        g = gcd(abs(a), abs(b)) or 1
        a, b = a // g, b // g
        out = f"{a}/{b}" if b != 1 else str(a)

    # 小数位数（用 ROUND_HALF_UP，跟学校教的四舍五入一致；Python 的 round 是银行家舍入）
    d = hint.get("decimals")
    if isinstance(d, int):
        v = _num(out)
        if v is not None:
            from decimal import Decimal, ROUND_HALF_UP
            q = Decimal("1") if d == 0 else Decimal(1).scaleb(-d)
            out = str(Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP))
            if d == 0:
                out = out.split(".")[0]

    # 百分数：题目要百分数时，0.25 这种小数按"化成百分数"处理（0.25 → 25%）
    if hint.get("percent") and "%" not in out:
        v = _num(out)
        if v is not None:
            if 0 < abs(v) <= 1:
                v *= 100
            out = f"{v:g}%"

    # 分数偏好：小数→分数（只在能整除逼近时）
    if hint.get("fraction") and re.fullmatch(r"-?\d+(\.\d+)?", out):
        from fractions import Fraction
        v = _num(out)
        if v is not None:
            fr = Fraction(v).limit_denominator(1000)
            if fr.denominator != 1 and abs(float(fr) - v) < 1e-9:
                out = f"{fr.numerator}/{fr.denominator}"
    return out
