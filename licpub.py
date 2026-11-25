"""Offline verifier for license tickets (RS256 / PKCS#1 v1.5, SHA-256).

Only public numbers live here; the private key stays in server/license_priv.json.
"""
from __future__ import annotations

import hashlib
import json
import time

PUB_N = 20427024475805347526728815962544223556158163190261975473439896355658228645092122150247782292209517643600651679397939464324849554143927866381314109330550632505768400517266598239265284409458358173295623796001614847491745227086222927089500200544474354718128629693197875037141052505245614406715242547802300478443544221918028611700056109912304205549067935543406054466121153842265137021880130106171803530134485606700626987904133267181807288565167442395958139931634785343108498732548649885106098158783121531490409084812803550495189522002003416073334258426452869768900409391101843209651160271770851833532086021635606797959353
PUB_E = 65537
DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _em(message: bytes, k: int) -> bytes:
    t = DIGEST_INFO + hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def verify_ticket(ticket: str, machine: str) -> dict:
    try:
        body, sig_hex = (ticket or "").split(".", 1)
        k = (PUB_N.bit_length() + 7) // 8
        got = pow(int(sig_hex, 16), PUB_E, PUB_N).to_bytes(k, "big")
        if got != _em(body.encode(), k):
            return {"ok": False, "error": "bad signature"}
        payload = json.loads(_b64d(body))
        if (payload.get("m") or "").strip().upper() != (machine or "").strip().upper():
            return {"ok": False, "error": "machine mismatch"}
        exp = float(payload.get("e") or 0)
        if exp and exp < time.time():
            return {"ok": False, "error": "expired"}
        return {"ok": True, "key": payload.get("k"), "expires_at": exp}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "bad ticket: %s" % exc}


def _b64d(text: str) -> bytes:
    import base64
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)
