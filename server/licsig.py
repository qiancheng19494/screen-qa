"""RSA PKCS#1 v1.5 (SHA-256) ticket signing -- pure stdlib.

Private key lives in server/license_priv.json (gitignored). Public numbers are
embedded in the desktop client (screen-qa/licensing.py).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PRIV_PATH = Path(__file__).resolve().parent / "license_priv.json"
DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _key():
    data = json.loads(PRIV_PATH.read_text(encoding="utf-8"))
    return int(data["n"]), int(data["e"]), int(data["d"])


def _em(message: bytes, k: int) -> bytes:
    t = DIGEST_INFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise ValueError("modulus too short")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def available() -> bool:
    return PRIV_PATH.exists()


def sign(message: bytes) -> str:
    n, e, d = _key()
    k = (n.bit_length() + 7) // 8
    sig = pow(int.from_bytes(_em(message, k), "big"), d, n)
    return sig.to_bytes(k, "big").hex()


def verify(message: bytes, sig_hex: str) -> bool:
    n, e, _ = _key()
    k = (n.bit_length() + 7) // 8
    try:
        sig = int(sig_hex, 16)
    except ValueError:
        return False
    got = pow(sig, e, n).to_bytes(k, "big")
    return got == _em(message, k)
