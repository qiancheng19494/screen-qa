"""代码题深度体检：一次找出多处错误 → 给修正代码 → 本地 compile 验证 → 不合格自动再修一轮。

对应用户的真实作业（COS-107 HW4 Errors：一个 Section 常有 5-7 处语法错）。
"""
from __future__ import annotations

import re

SWEEP_SYSTEM = """你是 Python 助教，任务是把学生这段代码里**所有**错误一次找出来。

按下面格式输出，标题原样保留：

## 错误清单
1) 第 N 行：`原样片段` —— 错误类型（如 SyntaxError/NameError/缩进）
2) 第 M 行：...
（有几处写几处，不要漏；如果某行在 OCR 里被并到上一行，按你能判断的位置说明）

## 修正后的代码
```python
(完整可运行代码，保持学生原有变量名与结构，只改必要处)
```

## 关键改动
用 1-3 句话说明改了哪几类问题。

要求：
- 只针对 Python 语法与明显逻辑错误，不要重写题目意图。
- 不要输出 LaTeX 或数学演算。
- 代码缩进必须是 4 个空格。
"""


def _pick_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text or "", re.S)
    return (m.group(1) if m else "").strip("\n")


def _pick_issues(text: str) -> list[str]:
    m = re.search(r"##\s*错误清单\s*\n(.*?)(?:\n##|\Z)", text or "", re.S)
    if not m:
        return []
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    return lines[:12]


def sweep(student_code: str, client, model: str = "", rounds: int = 2) -> dict:
    """返回 {issues, code, ok, rounds, note, raw}；不抛异常。"""
    from codingq import check_syntax
    model = model or ""
    ctx = student_code.strip()
    issues: list[str] = []
    code = ""
    ok = False
    note = ""
    used = 0
    for i in range(max(1, rounds)):
        user = ctx if i == 0 else (
            f"{ctx}\n\n（上一轮你给的修正代码仍然报错：{note}。请只针对这个错误再修一次，"
            f"其余保持不变。）")
        try:
            reply = client.chat(
                [{"role": "system", "content": SWEEP_SYSTEM},
                 {"role": "user", "content": user}],
                temperature=0.0, max_tokens=1600, model=model or None)
        except Exception as exc:  # noqa: BLE001
            return {"issues": issues, "code": code, "ok": False,
                    "rounds": used, "note": f"调用失败：{exc}", "raw": ""}
        used += 1
        found = _pick_issues(reply)
        if found:
            issues = found
        cand = _pick_code(reply)
        if cand:
            code = cand
        ok, note = check_syntax(code) if code else (False, "没有给出代码")
        if ok:
            break
    return {"issues": issues, "code": code, "ok": ok, "rounds": used,
            "note": note, "raw": reply if used else ""}
