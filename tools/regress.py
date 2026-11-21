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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as A          # noqa: E402
import config as C       # noqa: E402


def _norm(s: str) -> str:
    return "".join(str(s or "").split()).lower().replace("−", "-")


def _hit(expects, res) -> bool:
    hay = _norm(res.answer) + "|" + _norm(getattr(res, "fill", ""))
    return all(_norm(e) in hay for e in expects)


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
    ok = _hit(case["expect"], res)
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
    used = [r for r in rows if not r.get("err")]
    total_t = sum(r["elapsed"] for r in used)
    total_tok = sum(r["tokens"] for r in used)
    cost = C.cost_of(total_tok, 0, cfg)
    print("-" * 78)
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
