#!/usr/bin/env python3
"""License key CLI.

  python3 genkey.py new [--days 365] [--note 张三] [--machines 1]
  python3 genkey.py list
  python3 genkey.py revoke SQA-XXXX-XXXX-XXXX
"""
import argparse
import sys

import licensing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["new", "list", "revoke"])
    ap.add_argument("key", nargs="?")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--note", default="")
    ap.add_argument("--machines", type=int, default=1)
    a = ap.parse_args()
    licensing.init()
    if a.cmd == "new":
        k = licensing.new_key(days=a.days, note=a.note, machine_limit=a.machines)
        print(k)
        print(f"  有效期 {a.days} 天 · 备注 {a.note or '-'} · 可绑机器 {a.machines}")
    elif a.cmd == "list":
        rows = licensing.list_licenses(500)
        if not rows:
            print("(empty)")
            return 0
        print(f"{'KEY':22s} {'STATUS':8s} {'MACHINE':22s} {'EXPIRES':11s} NOTE")
        import time
        for r in rows:
            exp = time.strftime("%Y-%m-%d", time.localtime(float(r["expires_at"] or 0))) if r["expires_at"] else "-"
            print(f"{r['key']:22s} {r['status']:8s} {(r['machine'] or '-')[:22]:22s} {exp:11s} {r['note']}")
    else:
        if not a.key:
            print("need key")
            return 2
        print("revoked" if licensing.revoke(a.key) else "no such key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
