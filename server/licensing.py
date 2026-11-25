"""One-machine-one-key licensing (pure stdlib: HMAC-SHA256 tickets).

Table: licenses / license_events inside sqa.db
Ticket: base64url(payload).hmac   payload = {k:key, m:machine, e:exp, i:issued}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import time

import store

SECRET = os.environ.get("SQA_LICENSE_SECRET", "")
SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "license.key")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I/O/0/1


def secret() -> bytes:
    global SECRET
    if SECRET:
        return SECRET.encode()
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE, "rb").read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    SECRET = key
    return key.encode()


def init() -> None:
    con = store.connect()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          key TEXT UNIQUE NOT NULL,
          machine TEXT DEFAULT '',
          machine_limit INTEGER DEFAULT 1,
          note TEXT DEFAULT '',
          status TEXT DEFAULT 'new',
          created_at REAL NOT NULL,
          activated_at REAL DEFAULT 0,
          expires_at REAL DEFAULT 0,
          last_seen REAL DEFAULT 0,
          seen_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS license_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          key TEXT NOT NULL,
          ts REAL NOT NULL,
          action TEXT NOT NULL,
          machine TEXT DEFAULT '',
          ip TEXT DEFAULT '',
          detail TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_lic_machine ON licenses(machine);
        """)
        con.commit()
    finally:
        con.close()


def new_key(days: int = 365, note: str = "", machine_limit: int = 1) -> str:
    con = store.connect()
    try:
        while True:
            raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
            key = "SQA-" + "-".join(raw[i:i + 4] for i in range(0, 12, 4))
            if not con.execute("select 1 from licenses where key = ?", (key,)).fetchone():
                break
        now = time.time()
        con.execute(
            "insert into licenses(key, machine_limit, note, status, created_at, expires_at)"
            " values(?,?,?,?,?,?)", (key, machine_limit, note, "new", now, now + days * 86400))
        con.commit()
        return key
    finally:
        con.close()


def _event(con, key: str, action: str, machine: str, ip: str, detail: str = "") -> None:
    con.execute("insert into license_events(key, ts, action, machine, ip, detail) values(?,?,?,?,?,?)",
                (key, time.time(), action, machine, ip, detail))


def activate(key: str, machine: str, ip: str = "") -> dict:
    key = (key or "").strip().upper()
    machine = (machine or "").strip().upper()
    if not key or not machine:
        return {"ok": False, "error": "missing key or machine"}
    con = store.connect()
    try:
        row = con.execute("select * from licenses where key = ?", (key,)).fetchone()
        if not row:
            _event(con, key, "activate-fail", machine, ip, "unknown key")
            con.commit()
            return {"ok": False, "error": "unknown key"}
        now = time.time()
        if row["status"] == "revoked":
            _event(con, key, "activate-fail", machine, ip, "revoked")
            con.commit()
            return {"ok": False, "error": "revoked"}
        if float(row["expires_at"] or 0) and float(row["expires_at"]) < now:
            _event(con, key, "activate-fail", machine, ip, "expired")
            con.commit()
            return {"ok": False, "error": "expired"}
        bound = (row["machine"] or "").strip().upper()
        if bound and bound != machine:
            _event(con, key, "activate-fail", machine, ip, "bound to other machine")
            con.commit()
            return {"ok": False, "error": "this key is bound to another machine"}
        con.execute("update licenses set machine = ?, status = 'active', activated_at = ?, last_seen = ?,"
                    " seen_count = seen_count + 1 where id = ?",
                    (machine, float(row["activated_at"] or now), now, int(row["id"])))
        _event(con, key, "activate-ok" if not bound else "reactivate", machine, ip)
        con.commit()
        exp = float(row["expires_at"] or 0)
        return {"ok": True, "key": key, "machine": machine, "expires_at": exp,
                "days_left": max(0, int((exp - now) / 86400)) if exp else 0,
                "ticket": issue_ticket(key, machine, exp)}
    finally:
        con.close()


def check(key: str, machine: str, ip: str = "") -> dict:
    key = (key or "").strip().upper()
    machine = (machine or "").strip().upper()
    con = store.connect()
    try:
        row = con.execute("select * from licenses where key = ?", (key,)).fetchone()
        now = time.time()
        if not row:
            return {"ok": False, "error": "unknown key"}
        if row["status"] == "revoked":
            return {"ok": False, "error": "revoked"}
        if (row["machine"] or "").strip().upper() != machine:
            return {"ok": False, "error": "machine mismatch"}
        exp = float(row["expires_at"] or 0)
        if exp and exp < now:
            return {"ok": False, "error": "expired", "expires_at": exp}
        con.execute("update licenses set last_seen = ?, seen_count = seen_count + 1 where id = ?",
                    (now, int(row["id"])))
        _event(con, key, "check", machine, ip)
        con.commit()
        return {"ok": True, "key": key, "expires_at": exp,
                "days_left": max(0, int((exp - now) / 86400)) if exp else 0,
                "ticket": issue_ticket(key, machine, exp)}
    finally:
        con.close()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_ticket(key: str, machine: str, expires_at: float) -> str:
    """RS256 ticket if the private key exists, else legacy HMAC."""
    payload = {"k": key, "m": machine, "e": float(expires_at or 0), "i": time.time()}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    try:
        import licsig
        if licsig.available():
            return body + "." + licsig.sign(body.encode())
    except Exception:
        pass
    sig = _b64(hmac.new(secret(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def verify_ticket(ticket: str, machine: str) -> dict:
    try:
        body, sig = (ticket or "").split(".", 1)
        want = _b64(hmac.new(secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return {"ok": False, "error": "bad signature"}
        p = json.loads(_unb64(body))
        if (p.get("m") or "").strip().upper() != (machine or "").strip().upper():
            return {"ok": False, "error": "machine mismatch"}
        exp = float(p.get("e") or 0)
        if exp and exp < time.time():
            return {"ok": False, "error": "expired"}
        return {"ok": True, "key": p.get("k"), "expires_at": exp}
    except Exception as exc:
        return {"ok": False, "error": "bad ticket: %s" % exc}


def list_licenses(limit: int = 200) -> list[dict]:
    con = store.connect()
    try:
        rows = con.execute("select * from licenses order by id desc limit ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def revoke(key: str) -> bool:
    con = store.connect()
    try:
        cur = con.execute("update licenses set status = 'revoked' where key = ?", (key.strip().upper(),))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()

def rebind(key: str, machine: str = "", ip: str = "") -> dict:
    """Move a key to another machine (clears the binding). Returns status."""
    key = (key or "").strip().upper()
    con = store.connect()
    try:
        row = con.execute("select * from licenses where key = ?", (key,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown key"}
        con.execute("update licenses set machine = ?, activated_at = null, "
                    "status = 'new' where key = ?", ((machine or "").strip().upper(), key))
        con.execute("insert into license_events(key, ts, action, machine, ip, detail) "
                    "values (?, ?, 'rebind', ?, ?, ?)",
                    (key, time.time(), (machine or "").strip().upper(), ip, "rebind by admin"))
        con.commit()
        return {"ok": True, "key": key, "machine": (machine or "").strip().upper()}
    finally:
        con.close()


def extend(key: str, days: int = 30, ip: str = "") -> dict:
    """Push the expiry date forward by `days` (from now or current expiry)."""
    key = (key or "").strip().upper()
    con = store.connect()
    try:
        row = con.execute("select * from licenses where key = ?", (key,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown key"}
        base = float(row["expires_at"] or 0) or time.time()
        exp = max(base, time.time()) + max(1, int(days)) * 86400
        con.execute("update licenses set expires_at = ? where key = ?", (exp, key))
        con.execute("insert into license_events(key, ts, action, machine, ip, detail) "
                    "values (?, ?, 'extend', ?, ?, ?)",
                    (key, time.time(), row["machine"] or "", ip, "+%d days" % int(days)))
        con.commit()
        return {"ok": True, "key": key, "expires_at": exp,
                "days_left": int((exp - time.time()) // 86400)}
    finally:
        con.close()
