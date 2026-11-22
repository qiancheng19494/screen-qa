"""本地验算：把模型的答案代回题目里的式子，确认它真的成立。

为什么要有这一层：模型自我复核（R1 / 双模型）都可能一起错，而"把 x=6 代回
9x+6=60 看两边是否相等"是确定的算术。只有本模块能给出不依赖模型的正确性信号。

支持三类题：
1) 一元方程（含未知数）：9x + 6 = 60、3x^2 - 33x + 91 = 0、2(x+3) = 14
2) 已知 f(x) 求某点函数值：f(x) = 3x^2 - 33x + 91 且问 f(4)
3) 多解/多空：2, 3 这类答案逐个代入，每个都要成立

拿不准就返回 skip（绝不乱判 pass），调用方只把 fail 当"确实不对"。
"""

from __future__ import annotations

import math
import re

__all__ = ["verify", "check_equation_at", "extract_numbers"]

VAR = "x"

_TRANS = {
    "−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-",
    "×": "*", "·": "*", "⋅": "*", "÷": "/", "／": "/",
    "（": "(", "）": ")", "［": "[", "］": "]",
    "，": ",", "；": ";", "：": ":", "＋": "+", "－": "-", "＝": "=",
    "²": "^2", "³": "^3", "⁴": "^4", "½": "(1/2)",
    "√": "sqrt", "π": "pi", "∞": "inf",
}

_ALLOWED = re.compile(r"^[0-9a-zA-Z+\-*/^().,\s]*$")
_FUNC_TOK = re.compile(r"[a-zA-Z]+")
_FUNC_OK = {"x", "sin", "cos", "tan", "log", "ln", "exp", "abs", "sqrt", "pi"}
_FUNC_DEF = re.compile(r"(?:^|\s)([fghFGH])\s*\(\s*([a-zA-Z])\s*\)\s*=\s*([^;。\n]{1,80})", re.S)
_F_OF_NUM = re.compile(r"\b(?:f|g|h)\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)", re.S)
_EQ_LINE = re.compile(r"[^=\n]{1,80}=[^=\n]{1,80}")
_NUM_TOKEN = re.compile(r"-?\d+(?:\.\d+)?(?:\s*/\s*-?\d+(?:\.\d+)?)?")


class _SafeEval:
    """只允许 + - * / ^ 括号与几个常量/函数，用 eval 但锁死内建。"""

    FUNCS = {
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "ln": math.log, "exp": math.exp, "abs": abs,
        "pi": math.pi, "e": math.e,
    }

    @classmethod
    def run(cls, expr, x=None):
        s = (expr or "").strip()
        if not s:
            return None
        s = s.replace("^", "**")
        s = re.sub(r"(?<=[0-9)])\s*(?=[xX(])", "*", s)
        s = re.sub(r"(?<=[xX])\s*(?=\()", "*", s)
        s = re.sub(r"(?<=[0-9)])\s*(?=(?:sqrt|sin|cos|tan|log|ln|exp|abs))", "*", s)
        env = {"__builtins__": {}, "x": x if x is not None else 0.0}
        env.update(cls.FUNCS)
        try:
            return float(eval(s, env, {}))
        except Exception:
            return None


def _norm(s):
    return "".join(_TRANS.get(ch, ch) for ch in (s or "").replace("\u00a0", " "))


def _clean_expr(s):
    s = _norm(s).strip().strip(".,;:。")
    if not s:
        return None
    if not _ALLOWED.match(s):
        return None
    # 字母只能是变量 x 或白名单函数名（sqrt/sin/log…），防止把英文单词当表达式
    for tok in _FUNC_TOK.findall(s):
        if tok.lower() not in _FUNC_OK:
            return None
    return s


_CLAUSE_SPLIT = re.compile(r"\.\s+|:\s+")


def _drop_clause(s):
    """去掉题干里的指令性前缀：'Solve for x. 9x + 6' -> '9x + 6'。"""
    parts = [x for x in _CLAUSE_SPLIT.split(s or "") if x.strip()]
    if len(parts) <= 1:
        return s or ""
    for cand in reversed(parts):
        if re.search(r"[0-9](?:\s*[xX]|\b)|sqrt|\d\s*[/*+-]", cand, re.I):
            return cand
    return parts[-1]


def _expr_side(s, last):
    """先按"句子/冒号"切掉指令前缀，再清洗；失败时退回旧的正则截取。"""
    t = _clean_expr(_strip_words(_drop_clause(s)))
    if t:
        return t
    return _clean_expr(_strip_words(_math_side(s, last)))

_WORD_OK = {"sin", "cos", "tan", "log", "ln", "exp", "abs", "sqrt", "pi", "x"}


def _strip_words(s):
    """去掉混进表达式里的英文单词（Solve / Find / If …），保留函数名与变量。"""
    keep = []
    for tok in (s or "").split():
        low = tok.lower().strip(".,;:。")
        if not any(ch.isalpha() for ch in tok):
            keep.append(tok)
            continue
        if low in _WORD_OK or "x" in low:
            keep.append(tok)
            continue
        # 单词与数字粘连（"Solve2x"）时留数学部分
        math_part = "".join(ch for ch in tok if ch.isdigit() or ch in "xX+-*/^().,")
        if math_part:
            keep.append(math_part)
    return " ".join(keep)


_MATH_RUN = re.compile(r"[0-9xX+\-*/^(). ]+")


def _math_side(s, last):
    """取出紧贴等号的数学片段：左侧取最后一段，右侧取第一段（去掉 Solve/Find/: 之类）。"""
    runs = [r for r in _MATH_RUN.findall(s or "") if any(ch.isalnum() for ch in r)]
    if not runs:
        return ""
    return runs[-1] if last else runs[0]


def _equation_pairs(text):
    """把一段文本里所有 '左边=右边' 找出来（必须有 x，且两边都能解析成表达式）。"""
    eq = _norm(text)
    out = []
    for m in _EQ_LINE.finditer(eq):
        chunk = m.group(0)
        if chunk.count("=") != 1:
            continue
        left, right = chunk.split("=", 1)
        lx = _expr_side(left, True)
        rx = _expr_side(right, False)
        if not lx or not rx:
            continue
        if VAR not in lx.lower() and VAR not in rx.lower():
            continue
        if re.fullmatch(r"[a-zA-Z]", lx.strip()):
            continue          # "x = 4" 是给定条件，不是要检验的方程
        out.append((lx, rx))
    return out


def _equation_at(lx, rx, value):
    lv = _SafeEval.run(lx, value)
    rv = _SafeEval.run(rx, value)
    if lv is None or rv is None:
        return None
    if not (math.isfinite(lv) and math.isfinite(rv)):
        return None
    scale = max(1.0, abs(lv), abs(rv))
    return abs(lv - rv) <= 1e-6 * scale


def check_equation_at(lx, rx, value):
    return _equation_at(lx, rx, value)


def extract_numbers(text):
    """从答案文本里抽数值（支持 3/5、-2.5、1, 3 这种）。"""
    vals = []
    for tok in _NUM_TOKEN.findall(_norm(text)):
        tok = (tok or "").strip()
        try:
            if "/" in tok:
                a, b = [p.strip() for p in tok.split("/", 1)]
                if float(b) == 0:
                    continue
                vals.append(float(a) / float(b))
            else:
                vals.append(float(tok))
        except Exception:
            continue
    return vals


def _answer_values(answer, fill):
    for cand in (fill, answer):
        vals = extract_numbers(cand)
        if vals:
            return vals
    return []


_OPTION_LINE = re.compile(r"^\s*[A-Da-d]\s*[.)、．:：]")
_CHOICE_ANS = re.compile(r"^\s*[A-Da-d]\s*$")


def _drop_option_lines(text):
    """去掉 A. x = 2 这种选项行：选项里的等式不是题目条件，代回去会误判。"""
    kept = [ln for ln in text.splitlines() if not _OPTION_LINE.match(ln)]
    return "\n".join(kept)


def _verify_one(question, answer, fill=""):
    """返回 (状态, 说明)。状态 ∈ {ok, fail, skip}。"""
    q = _norm(question)
    if not q.strip():
        return "skip", "没有题目文本，跳过验算"
    if _ineq_question(q):
        return _ineq_check(q, answer, fill)
    # OCR 常把 "For y =" 粘成 "Fory ="：补回空格，后面 y = ... 的识别才成立
    q = re.sub(r"(?<=[a-z])([yg]\s*[=＝])", r" \1", q)
    vals = _answer_values(answer, fill)
    if not vals:
        return "skip", "答案里没有可代入的数值（可能是文字/选项）"
    if len(vals) > 6:
        return "skip", "答案数值过多，跳过验算"

    # 1) f(x) = ... 且题目里问了 f(某个数) → 直接算函数值比对
    fdefs = [m for m in _FUNC_DEF.finditer(q) if m.group(3).strip()]
    asked = _F_OF_NUM.findall(q)
    if fdefs and asked:
        raw = fdefs[0].group(3)
        expr = _clean_expr(re.split(r"[，,;]|求|find|what|when", raw)[0])
        try:
            arg = float(asked[0])
        except Exception:
            arg = None
        if expr and arg is not None:
            target = _SafeEval.run(expr, arg)
            if target is not None and math.isfinite(target):
                got = vals[0]
                scale = max(1.0, abs(target), abs(got))
                if abs(got - target) <= 1e-6 * scale:
                    return "ok", "f({0:g}) = {1:.6g}，与答案一致".format(arg, target)
                return "fail", "f({0:g}) 应为 {1:.6g}，但答案是 {2:.6g}".format(arg, target, got)

    # 1b) y = ... 且题目给了 x = k（"find y when x = 4"）→ 算 y(k) 比对
    m_y = re.search(r"(?:^|\s)y\s*=\s*([^;。,，\n]{1,80})", q)
    m_x = re.search(r"(?<!\d)x\s*=\s*(-?\d+(?:\.\d+)?)", q)
    if m_y and m_x and "y" not in m_y.group(1):
        expr = _clean_expr(re.split(r"[，,;]|find|what|when|evaluate", m_y.group(1))[0])
        if expr:
            try:
                arg = float(m_x.group(1))
                target = _SafeEval.run(expr, arg)
            except Exception:  # noqa: BLE001
                target = None
            if target is not None and math.isfinite(target):
                got = vals[0]
                scale = max(1.0, abs(target), abs(got))
                if abs(got - target) <= 1e-6 * scale:
                    return "ok", "x={0:g} 时 y={1:.6g}，与答案一致".format(arg, target)
                return "fail", "x={0:g} 时 y 应为 {1:.6g}，但答案是 {2:.6g}".format(
                    arg, target, got)

    # 2) 一元方程：候选值都要让方程成立
    eqs = _equation_pairs(q)
    if not eqs:
        return "skip", "题目里没有可解析的方程"
    for value in vals:
        flags = [_equation_at(lx, rx, value) for lx, rx in eqs]
        ok_flags = [f for f in flags if f is not None]
        if not ok_flags:
            return "skip", "方程里含无法求值的符号（如 ln/绝对值/分段）"
        if not any(ok_flags):
            lx, rx = eqs[0]
            lv, rv = _SafeEval.run(lx, value), _SafeEval.run(rx, value)
            lhs = "{0:.6g}".format(lv) if lv is not None else "?"
            rhs = "{0:.6g}".format(rv) if rv is not None else "?"
            return "fail", "把 {0}={1:g} 代回去，左边 {2} ≠ 右边 {3}".format(VAR, value, lhs, rhs)
    if len(vals) > 1:
        return "ok", "{0} 个值逐个代入都成立".format(len(vals))
    return "ok", "把 {0}={1:g} 代回去两边相等".format(VAR, vals[0])


# ---------- v2.13: 选择题选项代入 + 不等式验算 ----------
_OPT_LINE = re.compile(r"^\s*([A-Da-d])\s*[.)、．:：]\s*(.+?)\s*$", re.M)
_INEQ_ANS = re.compile(r"(?:^|\s)x\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)", re.I)
_INEQ_Q = re.compile(r"([0-9xX+\-*/^().\s]{3,60}?)\s*(<=|>=|<|>)\s*([0-9xX+\-*/^().\s]{1,60})")


def _options_of(text):
    out = {}
    for m in _OPT_LINE.finditer(text or ""):
        k = m.group(1).upper()
        if k not in out:
            out[k] = m.group(2)
    return out


def _option_value(txt):
    s = _norm(txt or "")
    s = re.sub(r"^[a-zA-Z]\s*=\s*", "", s).strip()
    s = s.replace("$", "").replace(",", "")
    return _SafeEval.run(_fix_sup(s), 0.0)


def _choice_check(question, letter):
    opts = _options_of(question)
    if not opts or letter not in opts:
        return None
    eqs = _equation_pairs(_drop_option_lines(_norm(question)))
    if not eqs:
        return None
    passed = []
    for k in sorted(opts):
        v = _option_value(opts[k])
        if v is None:
            continue
        flags = [_equation_at(lx, rx, v) for lx, rx in eqs]
        if flags and all(f is True for f in flags):
            passed.append(k)
    if not passed:
        return ("skip", "选项都没能代入成立，无法用验算判断选择题")
    if len(passed) == 1:
        k = passed[0]
        if k == letter:
            return ("ok", "只有 {0} 项满足题干（选项代入）".format(k))
        return ("fail", "代入题干只有 {0} 项成立，但答案是 {1}（选项代入）".format(k, letter))
    return ("skip", "有多项都成立（{0}），选项代入无法区分".format("".join(passed)))


def _ineq_question(q):
    for m in _INEQ_Q.finditer(q or ""):
        left, op, right = m.group(1).strip(), m.group(2), m.group(3).strip()
        left = left.lstrip(".:,;)]} ")
        right = right.rstrip(".:,;([{ ")
        if not left or not right:
            continue
        if VAR in left.lower() or VAR in right.lower():
            return left, op, right
    return None


def _ineq_check(question, answer, fill):
    ans = _INEQ_ANS.search(_norm(fill or answer or ""))
    if not ans:
        return ("skip", "不等式题，但答案里没有 x<k / x>k 形式")
    a_op, a_b = ans.group(1), float(ans.group(2))
    iq = _ineq_question(_norm(question))
    if not iq:
        return ("skip", "没解析到题干里的不等式")
    left, op, right = iq

    def f(x):
        lv = _SafeEval.run(left, x)
        rv = _SafeEval.run(right, x)
        if lv is None or rv is None:
            return None
        return lv - rv

    lo = hi = None
    prev = None
    for i in range(-1000, 1001, 5):
        v = f(float(i))
        if v is None:
            continue
        if prev is not None and (prev[1] == 0 or (prev[1] < 0) != (v < 0)):
            lo, hi = float(prev[0]), float(i)
            break
        prev = (i, v)
    if lo is None:
        return ("skip", "没找到不等式的分界点（可能无解或恒成立）")
    for _ in range(70):
        mid = (lo + hi) / 2.0
        vm = f(mid)
        if vm is None:
            break
        if vm == 0:
            lo = hi = mid
            break
        vlo = f(lo)
        if vlo is None:
            break
        if (vlo < 0) != (vm < 0):
            hi = mid
        else:
            lo = mid
    b = (lo + hi) / 2.0
    if abs(b - a_b) > 1e-3:
        return ("fail", "不等式分界点应为 {0:.6g}，但答案是 {1:.6g}".format(b, a_b))

    def holds(x):
        v = f(x)
        if v is None:
            return False
        return {"<": v < 0, ">": v > 0, "<=": v <= 0, ">=": v >= 0}[op]

    left_ok = holds(b - 1.0)
    right_ok = holds(b + 1.0)
    truth = "<" if (left_ok and not right_ok) else (">" if (right_ok and not left_ok) else None)
    if truth is None:
        return ("skip", "不等号方向无法判定（可能是 <= / >= 边界）")
    if a_op[0] != truth:
        return ("fail", "不等号方向应为 x {0} {1:.6g}，但答案是 x {2} {3:.6g}".format(truth, b, a_op, a_b))
    return ("ok", "分界点 {0:.6g} 与方向都对".format(b))


# ---------- end v2.13 ----------

def _fix_sup(text):
    """OCR 常把上标压平成 x2 / x3 / xA2，这里按幂处理（只对 x 后面做，避免误伤 2x）。"""
    text = re.sub(r"(?<=[xX])A([23])(?![0-9])", r"^\1", text)
    return re.sub(r"(?<=[xX])([23])(?![0-9])", r"^\1", text)


def verify(question, answer, fill=""):
    """多种 OCR 假设各试一遍：任一成立即 ok；仅在默认假设确实失败时才报 fail。

    背景：真实 OCR 会把 x 认成 ×（'9 × + 6 = 60'），也会把 x² 压平成 'x2'。
    """
    q = question or ""
    letter = ((fill or "").strip() or (answer or "").strip()).upper()
    if _CHOICE_ANS.match(letter):
        cr = _choice_check(question or "", letter)
        if cr:
            return cr
        return "skip", "选择题答案不代入验算"
    q = _drop_option_lines(q)
    variants = [q]
    for repl in ({"×": "x", "·": "x", "＊": "x", "乂": "x"},
                 {"十": "+"},                      # OCR 把 + 认成 十
                 {"×": "x", "·": "x", "十": "+"}):
        alt = q
        for k, val in repl.items():
            alt = alt.replace(k, val)
        if alt != q and alt not in variants:
            variants.append(alt)
    results = []
    for v in variants:
        results.append(_verify_one(v, answer, fill))
        sup = _fix_sup(v)
        if sup != v:
            results.append(_verify_one(sup, answer, fill))
    for r in results:
        if r[0] == "ok":
            return r
    base = results[0]
    if base[0] == "fail":
        return base
    for r in results:
        if r[0] == "fail":
            return r
    return base


