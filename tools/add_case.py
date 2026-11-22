"""把一张自己的题图加成回归用例。

用法：
  python tools/add_case.py --image C:/path/shot.png --answer "x = 6"
  python tools/add_case.py --image shot.png --answer 6 --mode value --note "第7题"

--mode text（默认）：期望串都要出现在"最终答案/填入答案"里
--mode value：期望的数值/分数命中任一即可（1e-6 容差）
--mode set：期望的多个值都得命中（顺序无关）

加完跑：python tools/regress.py --limit 99
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "cases"
CASES_JSON = ROOT / "tests" / "cases.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="题图路径（会被复制进 cases/）")
    ap.add_argument("--answer", required=True, help="期望答案（多个用逗号分隔）")
    ap.add_argument("--mode", default="text", choices=("text", "value", "set"))
    ap.add_argument("--note", default="")
    ap.add_argument("--name", default="", help="复制进 cases/ 后的文件名（默认沿用原名）")
    args = ap.parse_args()

    src = Path(args.image).expanduser()
    if not src.exists():
        print(f"找不到图片：{src}", file=sys.stderr)
        return 2

    CASES_DIR.mkdir(parents=True, exist_ok=True)
    dst = CASES_DIR / (args.name or src.name)
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)

    cases = []
    if CASES_JSON.exists():
        try:
            cases = json.loads(CASES_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cases = []

    # 图片相对路径：cases/ 里的用 cases/xxx.png，根目录的用 _xxx.png
    rel = f"cases/{dst.name}"
    try:
        rel = str(dst.relative_to(ROOT)).replace("\\", "/")
    except Exception:  # noqa: BLE001
        pass

    entry = {
        "image": rel,
        "expect": [x.strip() for x in str(args.answer).split(",") if x.strip()],
        "note": args.note or f"{dst.stem}（手动加入）",
    }
    if args.mode != "text":
        entry["mode"] = args.mode

    cases = [c for c in cases if c.get("image") != rel] + [entry]
    CASES_JSON.parent.mkdir(parents=True, exist_ok=True)
    CASES_JSON.write_text(json.dumps(cases, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"已加入：{rel}  期望={entry['expect']}  mode={args.mode}")
    print(f"现在共 {len(cases)} 条；跑 python tools/regress.py --limit 99 验证")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
