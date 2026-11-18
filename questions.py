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
