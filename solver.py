"""解题编排：OCR 文本 → 多次独立作答 → 多数投票 → 交叉审核。

为什么这样设计（这是把错误率压下来的关键）：
  * 单次作答的错误率取决于模型本身，无法保证 1%；
  * 多次独立采样 + 多数投票，能显著降低"偶发失误"；
  * 再让模型做一次"审核者"角色，看多个候选答案并裁决，能纠正
    多数样本一致但都错的系统性错误（例如看漏题目条件）；
  * 如果样本之间分歧大，会明确标注"结果存疑"，而不是假装确定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from deepseek_api import DeepSeekClient

FEISHU_SYSTEM = """你是一个严谨的解题专家。用户会给你一段从屏幕上识别出来的文字，里面可能是：
一道题目（选择题/填空题/问答题/编程题/数学题）、一份表格、一段代码或一段说明文字。

请遵守：
1. 只看给出的文字作答，不要臆造不存在的条件。
2. 如果文字明显是 OCR 误识别（乱码、断字、字符错位），先在心里纠正，不要向用户抱怨。
3. 如果信息确实不足以作答，直接说明缺什么，不要瞎猜。
4. 推理要简洁（选择题不用长篇大论），但必须真的算过/验证过。
5. 最后必须用下面的格式输出，最终答案只写一行：

## 推理
（你的解题过程，可多行）

## 最终答案
（一行，只给答案本身：选择题给选项字母+内容，填空题给横线上应填的内容，问答题给结论要点）"""

VERIFY_SYSTEM = """你是一位答案审核专家。下面会给出同一道题和若干个候选解答。
你的任务：独立判断哪个答案正确，或给出你认为正确的答案。

要求：
1. 独立复核每一步推理，尤其是计算、单位和题目问的到底是"哪个量"。
2. 不要因为多数候选答案一致就盲从——如果它们都错在同一个地方，你要纠正。
3. 如果题目本身信息不足，明确指出。
4. 输出格式：

## 复核
（简短指出各候选答案的问题或确认其正确）

## 最终答案
（一行，只给答案本身）"""

_ANS_RE = re.compile(r"##\s*最终答案\s*\n+(.+?)(?:\n|$)", re.S)
_ANS_INLINE_RE = re.compile(r"最终答案[：:]\s*(.+)")


def extract_answer(text: str) -> str:
    """从模型输出里抠出「最终答案」。"""
    if not text:
        return ""
    m = _ANS_RE.search(text)
    if m:
        return m.group(1).strip().strip("*` ")
    m = _ANS_INLINE_RE.search(text)
    if m:
        return m.group(1).strip().strip("*` ")
    # 没按格式输出时，取最后一行非空内容兜底
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _norm_key(answer: str) -> str:
    """用于投票比较的归一化 key。"""
    s = answer.strip().lower()
    s = re.sub(r"^[`*\"'\s]+|[`*\"'\s]+$", "", s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。；、,.;:：]+$", "", s)
    return s[:120]


@dataclass
class SolveResult:
    question_text: str
    answer: str = ""
    reasoning: str = ""
    samples: list[str] = field(default_factory=list)      # 每次采样的最终答案
    sample_details: list[str] = field(default_factory=list)
    votes: dict[str, int] = field(default_factory=dict)
    confidence: str = "未知"        # 高 / 中 / 低 / 存疑
    agreement: float = 0.0          # 一致度 0~1
    verified: bool = False
    elapsed: float = 0.0
    calls: int = 0
    error: str = ""
    warnings: list[str] = field(default_factory=list)


class Solver:
    def __init__(
        self,
        client: DeepSeekClient,
        samples: int = 3,
        verify: bool = True,
        temperature: float = 0.7,
        progress=None,
        escalate: bool = True,
        escalate_extra: int = 2,
    ):
        self.client = client
        self.samples = max(1, int(samples))
        self.verify = verify
        self.temperature = temperature
        self.progress = progress or (lambda msg: None)
        # 分歧时追加采样：平时 3 次便宜，遇到答案不一致再多问几次把错误压下去
        self.escalate = escalate
        self.escalate_extra = max(1, int(escalate_extra))

    # ------------------------------------------------------------------
    def _collect(self, messages, n: int, res: SolveResult, start: int = 0):
        """跑 n 次独立采样，返回 (答案, 原始输出, 失败次数)。"""
        answers: list[str] = []
        details: list[str] = []
        fails = 0
        for i in range(n):
            idx = start + i + 1
            self.progress(f"第 {idx}/{start + n} 次作答…")
            try:
                if self.samples == 1 and n == 1:
                    out = self.client.chat(messages, temperature=0.0)
                else:
                    out = self.client.chat(
                        messages, temperature=self.temperature, max_tokens=2048
                    )
            except Exception as exc:  # noqa: BLE001
                fails += 1
                res.error = str(exc)
                self.progress(f"第 {idx} 次调用失败：{exc}")
                continue
            res.calls += 1
            ans = extract_answer(out)
            if not ans:
                fails += 1
                res.error = "模型没有按格式给出「## 最终答案」"
                self.progress(f"第 {idx} 次输出格式异常")
                continue
            answers.append(ans)
            details.append(out)
        return answers, details, fails

    # ------------------------------------------------------------------
    def solve(self, question_text: str) -> SolveResult:
        import time

        t0 = time.time()
        res = SolveResult(question_text=question_text)
        messages = [
            {"role": "system", "content": FEISHU_SYSTEM},
            {"role": "user", "content": f"请解答下面的内容：\n\n{question_text}"},
        ]

        # 1) 多次独立采样（失败的样本不计入投票，绝不伪装成答案）
        ok_samples, ok_details, fail_count = self._collect(messages, self.samples, res)

        # 1b) 有分歧就追加采样：错误率比多花几毛钱重要
        if self.escalate and len(ok_samples) >= 2:
            quick: dict[str, int] = {}
            for a in ok_samples:
                k = _norm_key(a)
                quick[k] = quick.get(k, 0) + 1
            if max(quick.values()) < len(ok_samples):
                self.progress(f"答案有分歧，追加 {self.escalate_extra} 次采样再投票…")
                more_a, more_d, more_f = self._collect(
                    messages, self.escalate_extra, res, start=len(ok_samples))
                ok_samples += more_a
                ok_details += more_d
                fail_count += more_f

        if not ok_samples:
            res.answer = "（调用失败）"
            res.confidence = "存疑"
            res.agreement = 0.0
            res.elapsed = time.time() - t0
            res.error = res.error or "所有模型调用都失败了"
            return res

        res.samples = ok_samples
        res.sample_details = ok_details
        if fail_count:
            res.warnings.append(f"{fail_count} 次采样失败，已按成功样本投票")

        # 2) 统计投票
        votes: dict[str, int] = {}
        for a in res.samples:
            k = _norm_key(a)
            votes[k] = votes.get(k, 0) + 1
        res.votes = votes
        best_key = max(votes, key=lambda k: votes[k]) if votes else ""
        res.agreement = (votes.get(best_key, 0) / len(res.samples)) if res.samples else 0.0

        # 找到与最佳 key 对应的原始答案文本（保留格式）
        best_answer = ""
        for a in res.samples:
            if _norm_key(a) == best_key:
                best_answer = a
                break

        best_detail = ""
        for a, d in zip(res.samples, res.sample_details):
            if _norm_key(a) == best_key:
                best_detail = d
                break
        res.answer = best_answer
        res.reasoning = best_detail

        # 3) 交叉审核（样本有分歧，或始终开启）
        if self.verify and self.samples >= 1:
            self.progress("交叉审核中…")
            cand_lines = []
            for idx, (a, d) in enumerate(zip(res.samples, res.sample_details), 1):
                cand_lines.append(f"### 候选 {idx}\n{d.strip()[:1500]}")
            verify_msg = [
                {"role": "system", "content": VERIFY_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"【题目】\n{question_text}\n\n"
                        f"【候选解答】\n" + "\n\n".join(cand_lines)
                    ),
                },
            ]
            try:
                vout = self.client.chat(verify_msg, temperature=0.0, max_tokens=2048)
                res.calls += 1
                vans = extract_answer(vout)
                if vans and "失败" not in vans:
                    vkey = _norm_key(vans)
                    # 审核结果与多数答案不一致时，以审核为准（并降低置信度）
                    if vkey != best_key:
                        res.answer = vans
                        res.reasoning = vout
                        res.confidence = "中"
                        res.verified = True
                    else:
                        res.verified = True
            except Exception:  # noqa: BLE001
                pass

        # 4) 置信度
        if not res.confidence or res.confidence == "未知":
            if res.agreement >= 1.0 and res.samples:
                res.confidence = "高"
            elif res.agreement >= 0.66:
                res.confidence = "中"
            else:
                res.confidence = "存疑"

        if res.error and not res.answer:
            res.answer = "（调用失败）"
        # 有样本失败时，不再声称"高"置信度
        if res.error and res.confidence == "高":
            res.confidence = "中"

        res.elapsed = time.time() - t0
        return res
