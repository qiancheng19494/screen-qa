"""把 LaTeX 公式转成"人一眼能看懂"的 Unicode 文本。

思路来自朋友的项目 SpellCardHomework（MIT）里 modules/latex.py 的做法：
不追求排版完美，只把 $\\frac{a}{b}$、$\\sqrt{2}$、$x^{2}$ 这种源码变成
a/b、√2、x²，让答案框和详情里读得懂。这里按我们自己的用法重写，只保留
答题场景真正会遇到的几类：希腊字母、运算符、上下标、分式、根号、矩阵。
"""

from __future__ import annotations

import re

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

_SYM = {
    "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝", "infty": "∞", "partial": "∂",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "in": "∈", "notin": "∉",
    "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "sum": "∑", "prod": "∏",
    "int": "∫", "oint": "∮", "cdots": "…", "ldots": "…", "dots": "…",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋", "angle": "∠", "degree": "°", "circ": "°",
    "quad": " ", "qquad": "  ", "text": "", "mathrm": "", "left": "", "right": "",
    "displaystyle": "", "limits": "",
}

_SUP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
        "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "(": "⁽", ")": "⁾",
        "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
        "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
        "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ",
        "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ"}

_SUB = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
        "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "(": "₍", ")": "₎",
        "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ",
        "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
        "u": "ᵤ", "v": "ᵥ", "x": "ₓ"}


def _sup(s: str) -> str:
    if all(ch in _SUP for ch in s):
        return "".join(_SUP[ch] for ch in s)
    return "^" + (s if len(s) == 1 else "(" + s + ")")


def _sub(s: str) -> str:
    if all(ch in _SUB for ch in s):
        return "".join(_SUB[ch] for ch in s)
    return "_" + (s if len(s) == 1 else "(" + s + ")")


def _braced(s: str, i: int) -> tuple[str, int]:
    """从 s[i] 开始读一个 {...} 分组（或单个字符），返回 (内容, 结束位置)。"""
    if i >= len(s):
        return "", i
    if s[i] != "{":
        return s[i], i + 1
    depth, j = 0, i
    while j < len(s):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)


def latex_to_text(expr: str) -> str:
    """把一段 LaTeX 转成可读文本（不会抛异常）。"""
    if not expr:
        return ""
    s = expr.replace("\u2212", "-").replace("\u00a0", " ")

    # 去掉数学模式定界符
    s = re.sub(r"\$+", "", s)

    # \frac{a}{b} → a/b（分子分母短就直写，长的加括号）
    for _ in range(6):
        m = re.search(r"\\[dt]?frac\{", s)
        if not m:
            break
        start = m.start()
        num, p1 = _braced(s, s.index("{", start))
        # 跳过 \frac 后面的可选空组
        while p1 < len(s) and s[p1] == " ":
            p1 += 1
        if p1 >= len(s) or s[p1] != "{":
            break
        den, p2 = _braced(s, p1)
        num_s = latex_to_text(num) if "\\" in num else num
        den_s = latex_to_text(den) if "\\" in den else den
        num_s = num_s if len(num_s) <= 1 or " " not in num_s and "/" not in num_s else "(" + num_s + ")"
        den_s = den_s if len(den_s) <= 1 or " " not in den_s and "/" not in den_s else "(" + den_s + ")"
        s = s[:start] + f"{num_s}/{den_s}" + s[p2:]

    # \sqrt{x} → √x，\sqrt[3]{x} → ∛x
    for _ in range(6):
        m = re.search(r"\\sqrt(\[[0-9]+\])?\{", s)
        if not m:
            break
        start = m.start()
        idx = m.group(1)
        body, p = _braced(s, s.index("{", start))
        body_t = latex_to_text(body) if "\\" in body else body
        if idx:
            root = {"[3]": "∛", "[4]": "∜"}.get(idx, "^" + idx.strip("[]") + "√")
            rep = root + body_t
        else:
            rep = "√" + (body_t if len(body_t) <= 1 else "(" + body_t + ")")
        s = s[:start] + rep + s[p:]

    # 上标 / 下标
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _sup(m.group(1)), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: _sub(m.group(1)), s)
    s = re.sub(r"\^([0-9a-zA-Z+\-])", lambda m: _sup(m.group(1)), s)
    s = re.sub(r"_([0-9a-zA-Z+\-])", lambda m: _sub(m.group(1)), s)

    # 矩阵/分段：\begin{pmatrix} ... \end{pmatrix} → [1 2 3 4]
    s = re.sub(r"\\begin\{[a-zA-Z*]+\}", "[", s)
    s = re.sub(r"\\end\{[a-zA-Z*]+\}", "]", s)

    # 命令
    def _cmd(m: re.Match) -> str:
        name = m.group(1)
        if name in _GREEK:
            return _GREEK[name]
        if name in _SYM:
            return _SYM[name]
        return name  # 未知命令：去掉反斜杠，保留单词

    s = re.sub(r"\\([A-Za-z]+)", _cmd, s)
    # 转义字符
    s = s.replace("\\{", "{").replace("\\}", "}").replace("\\%", "%")
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    s = s.replace("\\\\", " ").replace("\\ ", " ")

    # 矩阵/分段：\begin{...} ... \end{...}
    # 收尾清理
    s = s.replace("{", "").replace("}", "").replace("&", " ")
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([,.;:)])", r"\1", s)
    return s.strip()


def pretty(text: str) -> str:
    """把整段文本里出现的 $...$ / \\(...\\) 公式片段转成可读符号。"""
    if not text:
        return ""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            if j < 0:
                out.append(text[i:])
                break
            out.append(latex_to_text(text[i + 2:j]))
            i = j + 2
            continue
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j < 0:
                out.append(latex_to_text(text[i + 1:]))
                break
            out.append(latex_to_text(text[i + 1:j]))
            i = j + 1
            continue
        if text.startswith("\\(", i):
            j = text.find("\\)", i + 2)
            if j < 0:
                out.append(latex_to_text(text[i + 2:]))
                break
            out.append(latex_to_text(text[i + 2:j]))
            i = j + 2
            continue
        out.append(text[i])
        i += 1
    res = "".join(out)
    return res if res == text else _tidy(res)


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        r"$x^{2} - 4x + 3 = 0$",
        r"$\frac{3}{5}$",
        r"$\sqrt{2x + 1} = 5$",
        r"$x \le -3$",
        r"$\sum_{i=1}^{n} i$",
        r"$\int_{0}^{1} x^{2}\,dx$",
        r"lim $x \to 0$ $\frac{\sin 3x}{5x}$",
        r"$\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}$",
        r"$\alpha \beta \pi \theta$ and $x^{-1}$",
    ]
    for t in tests:
        print(f"{t:<52} → {pretty(t)}")
