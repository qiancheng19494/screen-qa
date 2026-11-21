"""回归测试：把一批"已知答案"的题图跑一遍，报告正确率 / 耗时 / 花费。

用法：
    python tools/regress.py                 # 全部用例（默认不走缓存）
    python tools/regress.py --use-cache     # 允许命中缓存（快，但测不到模型）
    python tools/regress.py --samples 1 --fast --limit 3
    python tools/regress.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as A          # noqa: E402
import config as C       # noqa: E402


def _norm(s: str) -> str:
    return "".join(str(s or "").split()).lower().replace("\u2212", "-")


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?")


def _frac(tok: str):
    try:
        if "/" in tok:
            a, b = tok.split("/")
            return Fraction(a) / Fraction(b)
        return Fraction(tok)
    except Exception:  # noqa: BLE001
        return None


def _nums(text: str):
    out = []
    for m in _NUM_RE.finditer(str(text or "").replace("\u2212", "-")):
        f = _frac(m.group(0))
        if f is not None:
            out.append(f)
    return out


def _hit(expects, res, mode: str = "text") -> bool:
    """三种判定：
    text  —— 期望串都要在"答案|填入"里出现（原来 8 条用例的语义）
    value —— 期望值（数值/分数）出现在答案或填入里，允许 1e-6 误差
    set   —— 多值答案：填入里的数字集合要覆盖全部期望值（顺序无关）
    """
    hay = _norm(res.answer) + "|" + _norm(getattr(res, "fill", ""))
    if mode == "text":
        return all(_norm(e) in hay for e in expects)
    want = [_frac(str(e)) for e in expects]
    want = [w for w in want if w is not None]
    got = _nums(res.answer) + _nums(getattr(res, "fill", ""))
    if mode == "set":
        return all(any(abs(g - w) <= Fraction(1, 10**6) for g in got) for w in want)
    # value：任一期望值命中即可
    return any(any(abs(g - w) <= Fraction(1, 10**6) for g in got) for w in want)


def run_case(case: dict, cfg: dict, use_cache: bool) -> dict:
    image = ROOT / case["image"]
    if not image.exists():
        return {"case": case, "ok": False, "err": f"缺图：{image.name}"}
    c = dict(cfg)
    c["cache"] = bool(use_cache)
    events: list = []
    t0 = time.time()
    try:
        payload = A.run_pipeline(str(image), c, lambda *a: events.append(a))
    except Exception as exc:  # noqa: BLE001
        return {"case": case, "ok": False, "err": f"异常：{exc}"}
    res = payload.get("result")
    if res is None:
        return {"case": case, "ok": False, "err": "没有结果"}
    items = payload.get("items") or [res]
    mode = case.get("mode", "text")
    ok = _hit(case["expect"], res, mode)
    if len(items) > 1:  # 多题：每一题的答案都要出现在对应期望里（宽松匹配）
        joined = "|".join(_norm(i.answer) + "|" + _norm(i.fill or "") for i in items)
        ok = all(_norm(e) in joined for e in case["expect"])
    return {
        "case": case,
        "ok": ok,
        "answer": res.answer,
        "fill": res.fill,
        "confidence": res.confidence,
        "check": getattr(res, "check", ""),
        "calls": sum(int(getattr(i, "calls", 0) or 0) for i in items),
        "tokens": sum(int(getattr(i, "tokens_in", 0) or 0) + int(getattr(i, "tokens_out", 0) or 0)
                      for i in items),
        "elapsed": round(time.time() - t0, 2),
        "cached": bool(payload.get("cached")),
        "err": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "tests" / "cases.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--use-cache", action="store_true")
    ap.add_argument("--samples", type=int, default=0, help="覆盖采样次数")
    ap.add_argument("--fast", action="store_true", help="极速模式（1 次采样、不审核）")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.limit:
        cases = cases[: args.limit]
    cfg = C.load()
    if args.samples:
        cfg["samples"] = max(1, min(5, args.samples))
    if args.fast:
        cfg["fast"] = True
        cfg["verify"] = False
        cfg["escalate"] = False

    print(f"用例 {len(cases)} 条 · samples={cfg['samples']} · "
          f"缓存={'开' if args.use_cache else '关'} · 极速={'是' if args.fast else '否'}")
    print("-" * 78)
    rows = []
    for i, case in enumerate(cases, 1):
        row = run_case(case, cfg, args.use_cache)
        rows.append(row)
        if row.get("err"):
            print(f"{i:2}. ❌ {case['image']:<26} {row['err']}")
            continue
        mark = "✅" if row["ok"] else "❌"
        print(f"{i:2}. {mark} {case['image']:<26} 答案={row['answer'][:22]:<24} "
              f"填入={str(row['fill'])[:10]:<10} {row['confidence']} "
              f"验算={row['check'] or '-':<4} {row['elapsed']:>5.1f}s "
              f"{row['tokens']:>5}tok{'  [缓存]' if row['cached'] else ''}")
    ok = sum(1 for r in rows if r.get("ok"))
    ocr_limited = [r for r in rows
                   if not r.get("ok") and (r.get("case") or {}).get("known_ocr_limit")]
    used = [r for r in rows if not r.get("err")]
    total_t = sum(r["elapsed"] for r in used)
    total_tok = sum(r["tokens"] for r in used)
    cost = C.cost_of(total_tok, 0, cfg)
    print("-" * 78)
    if ocr_limited:
        names = "、".join((r["case"].get("image", "").split("/")[-1]) for r in ocr_limited)
        readable = len(rows) - len(ocr_limited)
        model_ok = ok
        print(f"其中受本地 OCR 限制（纯符号行丢失/系数被吞）：{len(ocr_limited)} 条 → {names}")
        print(f"在能读到的题面上：{model_ok}/{readable} = {model_ok / max(1, readable):.0%}")
    print(f"正确 {ok}/{len(rows)} = {ok / max(1, len(rows)):.0%} · "
          f"总耗时 {total_t:.1f}s · 平均 {total_t / max(1, len(used)):.1f}s/题 · "
          f"{total_tok} tokens · 约 ¥{cost:.4f}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"rows": rows, "ok": ok, "total": len(rows),
                        "elapsed": total_t, "tokens": total_tok},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.json}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
