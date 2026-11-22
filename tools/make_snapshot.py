"""把 50 条用例的"识别文本 + 答案"存成快照，之后可离线校验逻辑（0 元）。

用法：
  python tools/make_snapshot.py                 # 跑一遍真实 API，写 tests/snapshot.json
  python tools/make_snapshot.py --limit 6       # 只跑前 6 条
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

CASES = ROOT / "tests" / "cases.json"
SNAP = ROOT / "tests" / "snapshot.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    import app as A  # noqa: F401  (延迟导入：要能先设好路径)
    from config import load as load_cfg

    cases = json.loads(CASES.read_text(encoding="utf-8-sig"))
    if args.limit:
        cases = cases[: args.limit]

    out: dict[str, dict] = {}
    for i, c in enumerate(cases, 1):
        img = ROOT / c["image"] if not pathlib.Path(c["image"]).is_absolute() else pathlib.Path(c["image"])
        if not img.exists():
            img = ROOT / "cases" / pathlib.Path(c["image"]).name
        cfg = load_cfg()
        cfg.update({"samples": 1, "verify": True, "escalate": False,
                    "cache": False, "multi": False, "dual": False,
                    "reasoner": False, "adaptive": True, "sound": False,
                    "auto_copy": False})
        events: list[tuple] = []
        payload = A.run_pipeline(str(img), cfg, lambda *a: events.append(a))
        res = payload.get("result")
        out[pathlib.Path(c["image"]).name] = {
            "text": payload.get("text", ""),
            "answer": getattr(res, "answer", ""),
            "fill": getattr(res, "fill", ""),
            "check": getattr(res, "check", ""),
            "confidence": getattr(res, "confidence", ""),
            "elapsed": round(getattr(res, "elapsed", 0.0), 2),
            "expect": c.get("expect", []),
            "mode": c.get("mode", "text"),
            "known_ocr_limit": bool(c.get("known_ocr_limit")),
        }
        print(f"{i:>2}. {pathlib.Path(c['image']).name:<26} 答案={out[pathlib.Path(c['image']).name]['answer'][:18]:<18} 验算={out[pathlib.Path(c['image']).name]['check']}")

    SNAP.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n快照已写入 {SNAP}（{len(out)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
