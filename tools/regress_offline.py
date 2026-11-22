"""离线校验：用 tests/snapshot.json 里存下的识别文本与答案，重跑"匹配 + 代入验算"逻辑。

不调任何 API（0 元），用来在改代码后快速确认：判定逻辑、验算逻辑没被改坏。

用法：
  python tools/regress_offline.py
  python tools/regress_offline.py --verbose
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

SNAP = ROOT / "tests" / "snapshot.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not SNAP.exists():
        print("还没有快照：先跑 python tools/make_snapshot.py（会花少量 API 费）")
        return 2

    sys.path.insert(0, str(ROOT / "tools"))
    import regress as R  # 复用同一套匹配逻辑
    import verify as V

    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    rows, ok = [], 0
    for name, rec in snap.items():
        res = type("R", (), {
            "answer": rec.get("answer", ""),
            "fill": rec.get("fill", ""),
            "check": rec.get("check", ""),
        })()
        hit = R._hit(rec.get("expect") or [], res, rec.get("mode", "text"))
        # 验算：用快照里的识别文本重跑一次（纯本地）
        st, note = V.verify(rec.get("text", ""), rec.get("answer", ""), rec.get("fill", ""))
        limit = bool(rec.get("known_ocr_limit"))
        good = bool(hit) and (st != "fail")
        ok += 1 if good else 0
        rows.append((name, bool(hit), st, note, limit, rec.get("answer", "")))
        mark = "✅" if good else ("🟡" if limit else "❌")
        if args.verbose or not good:
            print(f" {mark} {name:<26} 匹配={bool(hit)} 验算={st:<4} {note[:36]} 答案={rec.get('answer','')[:16]}")

    total = len(rows)
    print("-" * 74)
    print(f"离线校验：{ok}/{total} = {ok / total:.0%}（0 元，未调用 API）")
    bad = [r for r in rows if not (r[1] and r[2] != "fail")]
    if bad:
        print("需要注意的条目：")
        for name, hit, st, note, limit, ans in bad:
            print(f"  · {name}: 匹配={hit} 验算={st} {note[:40]} {'(OCR 限制)' if limit else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
