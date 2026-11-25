"""Generate an RSA keypair (pure stdlib) for license tickets.

Writes server/license_priv.json (gitignored) and prints the public numbers that
must be embedded in the client (licensing.py: PUB_N / PUB_E).
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server" / "license_priv.json"
BITS = 2048
E = 65537


def is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def gen_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if is_probable_prime(cand):
            return cand


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    while True:
        p, q = gen_prime(BITS // 2), gen_prime(BITS // 2)
        if p == q:
            continue
        n = p * q
        phi = (p - 1) * (q - 1)
        if n.bit_length() != BITS:
            continue
        break
    d = pow(E, -1, phi)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"n": n, "e": E, "d": d, "bits": BITS}), encoding="utf-8")
    print("wrote", OUT)
    print("PUB_N =", n)
    print("PUB_E =", E)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
