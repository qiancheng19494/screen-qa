"""命令行同步：python tools/sync_cloud.py [--since 0]"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cloud  # noqa: E402
import config as C  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, default=0.0)
    args = ap.parse_args()
    cfg = C.load()
    base, tok = cfg.get("cloud_base", ""), cfg.get("cloud_token", "")
    if not (base and tok):
        print("先登录云端（设置 → 云端 → 登录/注册）")
        return 2
    recs = cloud.prepare_records(C.PROFILE / "records.jsonl", args.since)
    res = cloud.push(base, tok, recs, [])
    print("push:", res)
    got = cloud.pull(base, tok, args.since)
    print("pull records:", len(got.get("records") or []), "errors:", len(got.get("errors") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
