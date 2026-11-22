"""编程题（Python 代码纠错/补全）识别与本地校验。

背景：用户实际作业是 COS-107 的 Python 纠错题（Canvas + MyLab），
不是数学题。数学提示词会要求 LaTeX/代入验算，对代码题全部无效，
模型看起来就"不聪明"。这里单独给一套代码题提示词 + compile() 校验。
"""
from __future__ import annotations

import re

CODING_SYSTEM = """你是一个会逐行读代码的助教，专门帮学生找 Python 程序里的错误。

请严格按下面四段输出，段标题原样保留：

## 判断
一句话说清这是什么问题（例如：变量名拼写不一致、漏冒号、缩进层级错、函数未定义、类型不匹配）。

## 错在哪
指出具体行号或代码片段（引用学生代码里的原样文本），并解释为什么错。
如果有多个错误，按「1) 2) 3)」列出来，每条都要给位置。

## 怎么改
说明修改思路（一两句话），不要长篇大论。

## 修正后的代码
给出可以直接替换的完整代码或完整行，用 ```python 代码块包起来。
保持学生原本的变量名与结构，只改必须改的地方。

## 填入答案
如果题目要求"填写正确的行/答案"，这里只写可以直接打进去的内容（例如 `: ` 或 `city = "Beijing"`）；
如果是让整段重写，这里写"见上方代码"。多个空按顺序用逗号分隔。

注意：不要给数学演算，不要用 LaTeX，不要输出"最终答案 x = ..."这种格式。
"""

_PY_HINTS = (
    "python", "syntaxerror", "nameerror", "typeerror", "indentationerror",
    "valueerror", "traceback", "print(", "def ", "import ", "for ", "while ",
    "elif", "attributeerror", "keyerror", "indexerror", "unexpected eof",
    "invalid syntax", "未定义", "错误", "报错",
)
_CODE_LINE = re.compile(r"^\s{4,}\S|^\s*(?:def|class|if|elif|else|for|while|try|except|with)\b|=\s*\S+\(", re.M)


def is_coding(text: str) -> bool:
    """够粗但够用：命中 Python 关键字/异常名/缩进行，就按编程题处理。"""
    if not text:
        return False
    low = text.lower()
    hits = sum(1 for kw in _PY_HINTS if kw in low)
    code_lines = len(_CODE_LINE.findall(text))
    # 明显代码特征：缩进行 ≥2，或异常名 + 任一线索
    if code_lines >= 2:
        return True
    if "syntaxerror" in low or "indentationerror" in low or "traceback" in low:
        return True
    return hits >= 3 and ("def " in low or "print(" in low or "=" in text)


def check_syntax(code: str) -> tuple[bool, str]:
    """本地语法检查（只编译、不执行，安全）。返回 (是否通过, 错误信息)。"""
    if not code or not code.strip():
        return False, "没有代码可检查"
    try:
        compile(code, "<answer>", "exec")
        return True, ""
    except SyntaxError as exc:
        return False, f"第 {exc.lineno} 行：{exc.msg}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)


def extract_code(reply: str) -> str:
    """从模型回复里取第一段代码块。"""
    m = _FENCE.search(reply or "")
    return m.group(1).strip("\n") if m else ""
