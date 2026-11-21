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

import questions as qfmt
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
（一行，只给答案本身：选择题给选项字母+内容，填空题给横线上应填的内容，问答题给结论要点）

## 填入答案
（仅当题目是填空题 / 网页输入框（MyMathLab、Canvas 的 answer box 之类）时才写这一段：
 内容是**可以原样敲进输入框的字符串**——
 只写值或表达式本身，不要写"答案：""x="这类前缀（除非输入框明确要求写等式）；
 分数写 3/5 而不是 \\frac{3}{5}；带单位就带上单位；
 多个空按出现顺序用逗号分隔。
 选择题或不需要填框的题，这一段留空即可。）"""

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
（一行，只给答案本身）

## 填入答案
（若题目是填空题/网页输入框，给出可原样输入的字符串；否则留空）"""

_FILL_RE = re.compile(r"##\s*填入答案\s*\n+(.+?)(?:\n\s*##|$)", re.S)
_FILL_INLINE_RE = re.compile(r"填入答案[：:]\s*(.+)")


_FULL2HALF = {"，": ",", "；": ";", "：": ":", "（": "(", "）": ")",
              "－": "-", "−": "-", "–": "-", "％": "%", "　": " ",
              "．": ".", "０": "0", "１": "1", "２": "2", "３": "3",
              "４": "4", "５": "5", "６": "6", "７": "7", "８": "8", "９": "9"}


def clean_fill(raw: str) -> str:
    """把「填入答案」洗成能直接粘进输入框的样子。

    做三件事：全角标点/数字转半角；去掉 `x =` / `y =` 这种变量前缀
    （MyMathLab 的填空只认值本身）；压缩多余空格、去掉句末句点。
    复合值（如 `2, 3`、`(2,3)`、不等式 `x > 3`）保持原样。
    """
    if not raw:
        return ""
    s = "".join(_FULL2HALF.get(ch, ch) for ch in raw.strip())
    s = s.replace("$", "").strip()
    # 只有"变量 = 数值"这种简单形态才去掉前缀
    m = re.match(r"^[a-zA-Z]\s*=\s*(.+)$", s)
    if m and not re.search(r"[a-zA-Z]", m.group(1)):
        s = m.group(1).strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    s = s.rstrip("。.")
    return s[:200]


def extract_fill(text: str) -> str:
    """抠出「填入答案」（MyMathLab/Canvas 输入框要原样敲的值）。没写就返回空串。"""
    if not text:
        return ""
    m = _FILL_RE.search(text)
    raw = m.group(1) if m else None
    if raw is None:
        m2 = _FILL_INLINE_RE.search(text)
        raw = m2.group(1) if m2 else ""
    raw = (raw or "").strip().strip("*` ")
    raw = raw.split("\n")[0].strip()
    if raw in ("", "无", "—", "-", "不适用", "N/A"):
        return ""
    # 去掉模型偶尔加的前缀
    raw = re.sub(r"^填入(答案)?[：:]\s*", "", raw)
    return clean_fill(raw)

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
    fill: str = ""                 # 可原样敲进输入框的答案（MyMathLab / Canvas）
    reasoning: str = ""
    samples: list[str] = field(default_factory=list)      # 每次采样的最终答案
    sample_details: list[str] = field(default_factory=list)
    votes: dict[str, int] = field(default_factory=dict)
    confidence: str = "未知"        # 高 / 中 / 低 / 存疑
    agreement: float = 0.0          # 一致度 0~1
    verified: bool = False
    elapsed: float = 0.0
    calls: int = 0
    r1: bool = False               # 是否经过 deepseek-reasoner 复核
    dual: bool = False             # 是否做过「双模型互检」
    dual_note: str = ""            # 互检结论（一致 / 不一致改判 …）
    tokens_in: int = 0             # 本次求解消耗的 prompt tokens
    tokens_out: int = 0            # 本次求解消耗的 completion tokens
    check: str = ""                # 本地验算：ok / fail / skip
    check_note: str = ""           # 验算说明（"把 x=6 代回去两边相等"）
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
        reasoner: str = "",
        dual: bool = False,
    ):
        self.client = client
        self.samples = max(1, int(samples))
        self.verify = verify
        self.temperature = temperature
        self.progress = progress or (lambda msg: None)
        # 分歧时追加采样：平时 3 次便宜，遇到答案不一致再多问几次把错误压下去
        self.escalate = escalate
        self.escalate_extra = max(1, int(escalate_extra))
        # 低置信度时用更强推理模型复核一次（空字符串 = 不复核）
        self.reasoner = (reasoner or "").strip()
        # 双模型互检：不管置信度，始终让 R1 独立算一遍，两条链路一致才给🟢
        self.dual = bool(dual) and bool(self.reasoner)

    # ------------------------------------------------------------------
    def _collect(self, messages, n: int, res: SolveResult, start: int = 0):
        """并行跑 n 次独立采样，返回 (答案列表, 原始输出列表, 失败次数)。

        为什么并行：串行 3 次要把 3 个往返的延迟叠加（约 3×1.2 秒），
        并行只花最慢那一次的时间；采样之间本来就互不依赖，并行不损失独立性。
        """
        from concurrent.futures import ThreadPoolExecutor

        def one(idx: int):
            temp = 0.0 if (self.samples == 1 and n == 1) else self.temperature
            try:
                out = self.client.chat(messages, temperature=temp, max_tokens=2048)
                return idx, out, None
            except Exception as exc:  # noqa: BLE001
                return idx, None, exc

        answers: list[str] = []
        details: list[str] = []
        fails = 0
        self.progress(f"{n} 路并行作答中…")
        idxs = list(range(start + 1, start + n + 1))
        with ThreadPoolExecutor(max_workers=max(1, min(n, 8))) as pool:
            results = list(pool.map(one, idxs))

        for idx, out, exc in sorted(results, key=lambda r: r[0]):
            if exc is not None:
                fails += 1
                res.error = str(exc)
                self.progress(f"第 {idx} 次调用失败：{exc}")
                continue
            res.calls += 1
            ans = extract_answer(out or "")
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
        usage0 = dict(getattr(self.client, "usage_total", {}) or {})
        import time

        t0 = time.time()
        res = SolveResult(question_text=question_text)
        # 先把"这题要什么格式"抠出来，既塞进提示词，也对最终答案做后处理
        hints = qfmt.format_hint(question_text)
        hint_line = qfmt.hint_text(hints)
        user_text = f"请解答下面的内容：\n\n{question_text}"
        if hint_line:
            user_text += f"\n\n（本题答案格式要求：{hint_line}。填入答案必须严格符合这个格式。）"
        messages = [
            {"role": "system", "content": FEISHU_SYSTEM},
            {"role": "user", "content": user_text},
        ]

        def key_of(ans: str) -> str:
            """投票用的归一化：先洗成"要敲进输入框的值"再比。
            这样 x = 3.14 / 3.14 / 3.142857（同一题要求两位小数）会算同一票，
            不会因为写法不同被误判成"有分歧"。
            """
            try:
                return _norm_key(qfmt.apply_format(clean_fill(ans), hints))
            except Exception:  # noqa: BLE001
                return _norm_key(ans)

        # 1) 多次独立采样（失败的样本不计入投票，绝不伪装成答案）
        ok_samples, ok_details, fail_count = self._collect(messages, self.samples, res)

        # 1b) 有分歧就追加采样：错误率比多花几毛钱重要
        if self.escalate and len(ok_samples) >= 2:
            quick: dict[str, int] = {}
            for a in ok_samples:
                k = key_of(a)
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
            k = key_of(a)
            votes[k] = votes.get(k, 0) + 1
        res.votes = votes
        best_key = max(votes, key=lambda k: votes[k]) if votes else ""
        res.agreement = (votes.get(best_key, 0) / len(res.samples)) if res.samples else 0.0

        # 找到与最佳 key 对应的原始答案文本（保留格式）
        best_answer = ""
        for a in res.samples:
            if key_of(a) == best_key:
                best_answer = a
                break

        best_detail = ""
        for a, d in zip(res.samples, res.sample_details):
            if key_of(a) == best_key:
                best_detail = d
                break
        res.answer = best_answer
        res.reasoning = best_detail
        res.fill = extract_fill(best_detail)

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
                    vkey = key_of(vans)
                    # 审核结果与多数答案不一致时，以审核为准（并降低置信度）
                    if vkey != best_key:
                        res.answer = vans
                        res.reasoning = vout
                        res.fill = extract_fill(vout) or res.fill
                        res.confidence = "中"
                        res.verified = True
                    else:
                        res.verified = True
                        res.fill = res.fill or extract_fill(vout)
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

        # 4a) 双模型互检：不管置信度，让 R1 完全独立算一遍
        #     两条链路一致 → 才敢给"高"；不一致 → 追加采样打破平票，仍不一致就降"存疑"
        if (self.dual and res.answer and "调用失败" not in res.answer
                and res.confidence in ("高", "中")):
            self.progress("双模型互检中（R1 独立再算一遍，约 10-40 秒）…")
            try:
                dmsg = [
                    {"role": "system", "content": FEISHU_SYSTEM},
                    {"role": "user", "content": (
                        f"【题目】\n{question_text}\n\n"
                        "请独立作答。不要假设任何已有答案是对的，"
                        "一步步自己算，然后按格式给出最终答案。"
                    )},
                ]
                dout = self.client.chat(dmsg, temperature=0.0,
                                        max_tokens=3072, model=self.reasoner)
                res.calls += 1
                res.dual = True
                dans = extract_answer(dout)
                if dans and "失败" not in dans:
                    dkey = key_of(dans)
                    if dkey == best_key:
                        res.dual_note = f"双模型一致（{res.answer}）"
                        if res.agreement >= 0.999:
                            res.confidence = "高"
                    else:
                        res.dual_note = f"双模型不一致：投票 {res.answer} / R1 {dans}"
                        res.warnings.append(res.dual_note)
                        self.progress("两个模型结论不一致，追加采样再投票…")
                        more_a, more_d, _ = self._collect(
                            messages, self.escalate_extra, res,
                            start=len(res.samples))
                        res.samples = list(res.samples) + more_a
                        res.sample_details = list(res.sample_details) + more_d
                        votes2: dict[str, int] = {}
                        for a in res.samples:
                            k = key_of(a)
                            votes2[k] = votes2.get(k, 0) + 1
                        res.votes = votes2
                        best2 = max(votes2, key=lambda k: votes2[k]) if votes2 else ""
                        res.agreement = (votes2.get(best2, 0) / len(res.samples)
                                         if res.samples else 0.0)
                        if best2 == dkey:
                            res.answer = dans
                            res.reasoning = dout
                            res.confidence = "中"
                            res.verified = True
                        else:
                            res.answer = next(
                                (a for a in res.samples if key_of(a) == best2),
                                res.answer)
                            res.confidence = "存疑"
                            res.warnings.append(
                                "补充采样后仍与 R1 不一致 → 置信度降为存疑，请人工核对")
                        res.fill = (extract_fill(res.reasoning) or res.fill
                                    or clean_fill(res.answer))
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"双模型互检失败：{exc}")

        # 4b) 低置信度 → 换 deepseek-reasoner 复核一次
        #     为什么只在"中/存疑"时用：R1 一次要 10-40 秒，🟢高的题没必要多花钱
        if (self.reasoner and not res.dual and res.confidence in ("中", "存疑")
                and res.answer and "调用失败" not in res.answer):
            self.progress("R1 深度复核中（较慢，约 10-40 秒）…")
            try:
                rmsg = [
                    {"role": "system", "content": FEISHU_SYSTEM},
                    {"role": "user", "content": (
                        f"【题目】\n{question_text}\n\n"
                        f"【此前多数投票得到的答案】\n{res.answer}\n\n"
                        "请独立重新求解，不要被上面的答案带偏；"
                        "如果你认为它错了，直接给出正确答案并说明理由。"
                    )},
                ]
                rout = self.client.chat(rmsg, temperature=0.0,
                                        max_tokens=3072, model=self.reasoner)
                res.calls += 1
                res.r1 = True
                rans = extract_answer(rout)
                rfill = extract_fill(rout)
                if rans and "失败" not in rans:
                    rkey = key_of(rans)
                    if rkey != best_key:
                        res.warnings.append(f"R1 复核改判：{res.answer} → {rans}")
                        res.answer = rans
                        res.reasoning = rout
                        res.fill = rfill or clean_fill(rans)
                        res.confidence = "中"
                        res.verified = True
                    else:
                        # 两条独立链路给出一致结论：存疑可以升到中，但不能凭空变高
                        if res.confidence == "存疑":
                            res.confidence = "中"
                        res.fill = res.fill or rfill
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"R1 复核失败：{exc}")

        # 5) 按题目要求格式化"填入答案"（几位小数/最简分数/百分数/排序）
        if res.fill and hints:
            try:
                res.fill = qfmt.apply_format(res.fill, hints)
            except Exception:  # noqa: BLE001
                pass

        # 6) 本地验算：把答案代回原式，不依赖模型的独立正确性信号
        try:
            import verify as _vfy
            res.check, res.check_note = _vfy.verify(question_text, res.answer, res.fill)
        except Exception as exc:  # noqa: BLE001
            res.check, res.check_note = "skip", f"验算不可用：{exc}"
        try:
            import ocrcheck
            res.warnings.extend(ocrcheck.audit(question_text, res.answer, res.fill,
                                               res.reasoning))
        except Exception:  # noqa: BLE001
            pass
        if res.check == "fail":
            res.warnings.append(f"本地验算不通过：{res.check_note}")
            if res.confidence == "高":
                res.confidence = "中"
            elif res.confidence == "中":
                res.confidence = "存疑"

        usage1 = dict(getattr(self.client, "usage_total", {}) or {})
        res.tokens_in = int(usage1.get("prompt", 0)) - int(usage0.get("prompt", 0))
        res.tokens_out = int(usage1.get("completion", 0)) - int(usage0.get("completion", 0))

        res.elapsed = time.time() - t0
        return res
