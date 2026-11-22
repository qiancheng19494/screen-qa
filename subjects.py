"""题型路由：按内容选系统提示词（数学以外的题不该被套数学格式）。

设计原则
1. 数学题继续用 solver.FEISHU_SYSTEM（已调好，绝不动）。
2. 编程题继续用 codingq.CODING_SYSTEM（带 compile 校验）。
3. 其余学科给专用骨架，但共用同一个输出契约：
   ## 题型 / ## 思路 / ## 解答 / ## 填入答案
   —— 这样前端的"填入答案 / 复制答案 / 置信度"全都能复用。
4. 检测宁可保守：拿不准就掉进 math 或 general，不做花哨猜测。
"""
from __future__ import annotations

import re

import codingq

_CONTRACT = """严格的输出格式（少写标题、不要寒暄）：

## 题型
一句话说明这是什么学科、什么类型的题。

## 思路
最多 6 行，写清楚关键判断依据。不要复述题干。

## 解答
给完整结论。分点写，能用公式就用公式，能用列表就用列表。

## 填入答案
只写"能原样提交/粘贴"的那部分：数值带单位、答案选项、改好的句子或代码。
多空按顺序用逗号分隔；没有可填内容就写 "见解答"。"""

_LATEX_HINT = """
公式用纯文本写法（x^2、sqrt(2)、1/2、pi），不要用 $...$ 包裹，不要输出 LaTeX 环境。"""

_SYS = {}

_SYS["physics"] = """你是物理题的解题助手，学生用中文提问、题目多为英文。""" + _CONTRACT + """
额外要求：
- 先列已知量与所求量（带单位），再写公式，再代入数值，最后给结果。
- 结果必须带单位；单位明显不合理时（如速度算出 1e8 m/s）在"思路"里提示检查。
- 涉及方向、符号、坐标系时写清楚取哪个方向为正。""" + _LATEX_HINT

_SYS["chemistry"] = """你是化学题的解题助手。""" + _CONTRACT + """
额外要求：
- 写方程式必须配平，并在括号里标状态（s/l/g/aq）。
- 计算题先写配平后的关系式与摩尔比，再代数值。
- 遇到有机结构题，用文字描述官能团与位置，不要画图。
- 拿不准的异构体/命名，明确说"以下按 IUPAC 规则"并给依据。""" + _LATEX_HINT

_SYS["biology"] = """你是生物题的解题助手。""" + _CONTRACT + """
额外要求：
- 概念题：先给定义，再给一个具体例子，最后回答"所以本题选什么/为什么"。
- 遗传题：写清基因型、表现型、比例推算步骤。
- 不要编造论文或实验数据；不确定的机制明说不确定。"""

_SYS["statistics"] = """你是统计与数据分析题的助手。""" + _CONTRACT + """
额外要求：
- 先判断题目要什么（描述统计 / 区间估计 / 假设检验 / 回归 / 概率）。
- 写出用的公式或检验名称（如"双样本 t 检验"），列出前提假设是否满足。
- 计算给中间量（均值、标准差、n、t 值、自由度、p 值），最后给结论一句话。
- 结论要落到题目语境（"拒绝原假设，即两组差异显著"），不要只给数字。""" + _LATEX_HINT

_SYS["english"] = """你是英语学习助手，服务对象是中国学生（COS-107 类课程，题目常为英文）。""" + _CONTRACT + """
额外要求：
- 语法/改错题：逐条列"原句片段 → 改法 → 为什么（术语+中文解释）"。
- 翻译题：给一版自然译文，再点出 1–2 个关键搭配为什么这么译。
- 写作/润色题：先给修改后的完整段落，再列 3 条主要改动（不要逐字重写）。
- "填入答案"只放最终要提交的英文句子/单词，不要放解释。"""

_SYS["social"] = """你是人文社科（历史、社会、经济、哲学、法律常识）答题助手。""" + _CONTRACT + """
额外要求：
- 结论先行，然后给 3 条以内的论据，每条尽量带时间/人物/事件/机制。
- 叙述题给"提纲式"结构（论点 → 论据 → 反方视角 → 结论），不要写成长文。
- 有争议的历史评价，明确写"主流观点是…，另有观点认为…"。
- 不要编造具体年份、条约名、数据；不确定就写"具体年份需核对"。"""

_SYS["general"] = """你是通用解题助手，题目可能不属于任何单一学科。""" + _CONTRACT + """
额外要求：
- 先判断题目在问什么（概念解释 / 计算 / 判断对错 / 写作 / 推理）。
- 不套用数学演算模板；按题目本身的结构回答。
- 信息不足时，先列出你需要的 1–2 个关键信息，再给一个"在常见假设下"的答案。"""


def detect(text: str) -> str:
    """返回学科名；math 表示交给数学链路，code 表示交给编程链路。"""
    if not text:
        return "math"
    low = text.lower()
    if codingq.is_coding(text):
        return "code"
    # 明显的 Python 片段（真实作业常缺 def 的冒号，is_coding 会漏）
    if re.search(r"(?m)^\s*(?:def|class)\s+\w+", text) or (
            "print(" in low and ("def " in low or "input(" in low)):
        return "code"
    rules = [
        ("chemistry", (r"[A-Z][a-z]?\d*(?:\s*\+\s*[A-Z][a-z]?\d*)+\s*(?:->|→)",
                       r"h2o|nacl|co2|nh3|h2so4|fe2o3|al2o3|ch4",
                       r"\bmol\b|molar|配平|balance|reaction|化学|氧化|还原")),
        ("physics", (r"\bnewton|\bjoule|\bwatt|\bvolt|\bohm|\bvelocity|\bacceleration",
                     r"m/s|kg\b|n\b.*force|功率|电流|电压|电阻|加速度|速度|重力|摩擦|斜面|动量")),
        ("statistics", (r"standard deviation|confidence interval|p-value|hypothesis test",
                        r"regression|anova|correlation|mean of|median",
                        r"标准差|置信区间|假设检验|回归|方差|相关系数|中位数")),
        ("biology", (r"\bdna\b|\brna\b|\bcell\b|enzyme|protein|gene|allele|mitosis",
                     r"细胞|基因|酶|蛋白|遗传|染色体|光合|呼吸作用")),
        ("english", (r"\btranslate\b|\brewrite\b|\bgrammar\b|correct the sentence",
                     r"essay|paragraph|proofread|\bverb\b|\btense\b",
                     r"翻译|润色|语法|改错|病句|时态|主谓一致|英文写作")),
        ("social", (r"\bhistory\b|\bwar\b|revolution|constitution|treaty|\bempire\b",
                    r"历史|战争|革命|朝代|宪法|条约|经济政策|哲学|ethical|政治")),
    ]
    for name, pats in rules:
        for pat in pats:
            if re.search(pat, low):
                return name
    return "math"


def system_for(text: str) -> str | None:
    """编程/数学返回 None（由 solver 用各自既有提示词），其余返回专用提示词。"""
    kind = detect(text)
    if kind == "code":
        return codingq.CODING_SYSTEM
    if kind == "math":
        return None
    return _SYS.get(kind) or _SYS["general"]
