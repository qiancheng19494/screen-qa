"""SQLite 存储层：用户、令牌、用量、记录同步（纯标准库）。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("SQA_DB", Path(__file__).resolve().parent / "sqa.db"))
ITER = 120_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  quota_day INTEGER NOT NULL DEFAULT 300000,
  used_date TEXT NOT NULL DEFAULT '',
  used_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  model TEXT,
  pt INTEGER DEFAULT 0,
  ct INTEGER DEFAULT 0,
  cost REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
  user_id INTEGER NOT NULL,
  uid TEXT NOT NULL,
  ts REAL NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (user_id, uid)
);
CREATE TABLE IF NOT EXISTS errors (
  user_id INTEGER NOT NULL,
  uid TEXT NOT NULL,
  ts REAL NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (user_id, uid)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), ITER).hex()


def register(name: str, pw: str) -> tuple[str | None, str]:
    """返回 (token, 错误信息)。"""
    name = (name or "").strip()
    if not (2 <= len(name) <= 20):
        return None, "用户名 2-20 个字符"
    if len(pw or "") < 6:
        return None, "密码至少 6 位"
    salt = secrets.token_hex(16)
    with connect() as con:
        try:
            cur = con.execute(
                "INSERT INTO users(name, salt, pw_hash, created_at) VALUES(?,?,?,?)",
                (name, salt, _hash(pw, salt), time.time()),
            )
        except sqlite3.IntegrityError:
            return None, "这个用户名已经注册了"
        uid = int(cur.lastrowid)
        token = secrets.token_urlsafe(32)
        con.execute("INSERT INTO tokens(token, user_id, created_at) VALUES(?,?,?)",
                    (token, uid, time.time()))
    return token, ""


def login(name: str, pw: str) -> tuple[str | None, str]:
    with connect() as con:
        row = con.execute("SELECT id, salt, pw_hash FROM users WHERE name=?",
                          ((name or "").strip(),)).fetchone()
        if not row:
            return None, "用户名或密码不对"
        if not secrets.compare_digest(row["pw_hash"], _hash(pw or "", row["salt"])):
            return None, "用户名或密码不对"
        token = secrets.token_urlsafe(32)
        con.execute("INSERT INTO tokens(token, user_id, created_at) VALUES(?,?,?)",
                    (token, int(row["id"]), time.time()))
    return token, ""


def user_by_token(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    with connect() as con:
        return con.execute(
            "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token=?",
            (token,)).fetchone()


def take_quota(user: sqlite3.Row, tokens: int) -> tuple[bool, int]:
    """按天限额；返回 (是否允许, 今日剩余)。"""
    today = time.strftime("%Y-%m-%d")
    used = int(user["used_tokens"]) if user["used_date"] == today else 0
    left = int(user["quota_day"]) - used
    if tokens <= 0:
        return True, max(left, 0)
    if tokens > left:
        return False, max(left, 0)
    with connect() as con:
        con.execute("UPDATE users SET used_date=?, used_tokens=? WHERE id=?",
                    (today, used + tokens, int(user["id"])))
    return True, left - tokens


def add_usage(uid: int, model: str, pt: int, ct: int, cost: float) -> None:
    with connect() as con:
        con.execute("INSERT INTO usage(user_id, ts, model, pt, ct, cost) VALUES(?,?,?,?,?,?)",
                    (uid, time.time(), model, pt, ct, cost))


def stats(uid: int) -> dict:
    today = time.strftime("%Y-%m-%d")
    with connect() as con:
        tot = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost),0) c, COALESCE(SUM(pt+ct),0) t "
            "FROM usage WHERE user_id=?", (uid,)).fetchone()
        day = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost),0) c, COALESCE(SUM(pt+ct),0) t "
            "FROM usage WHERE user_id=? AND date(ts,'unixepoch','localtime')=?",
            (uid, today)).fetchone()
    return {"total": dict(tot), "today": dict(day)}


def daily_usage(uid: int, days: int = 30) -> list[dict]:
    """\u6309\u5929\u6c47\u603b\u7528\u91cf\uff08\u6700\u8fd1 N \u5929\uff09\u3002"""
    con = connect()
    try:
        rows = con.execute(
            "select date(ts, 'unixepoch', 'localtime') as d, sum(cost) as cost, count(*) as n"
            " from usage where user_id = ? group by d order by d desc limit ?",
            (uid, days)).fetchall()
        return [{"date": r["d"], "cost": round(float(r["cost"] or 0), 6), "n": int(r["n"])} for r in rows][::-1]
    finally:
        con.close()


def push_records(uid: int, table: str, items: list[dict]) -> int:
    if table not in ("records", "errors"):
        raise ValueError("bad table")
    n = 0
    with connect() as con:
        for it in items:
            rid = str(it.get("uid") or it.get("id") or "")
            if not rid:
                continue
            ts = float(it.get("ts") or time.time())
            payload = json.dumps(it, ensure_ascii=False)
            con.execute(
                f"INSERT INTO {table}(user_id, uid, ts, payload) VALUES(?,?,?,?) "
                f"ON CONFLICT(user_id, uid) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
                (uid, rid, ts, payload))
            n += 1
    return n


def pull_records(uid: int, table: str, since: float = 0.0, limit: int = 500) -> list[dict]:
    if table not in ("records", "errors"):
        raise ValueError("bad table")
    with connect() as con:
        rows = con.execute(
            f"SELECT payload FROM {table} WHERE user_id=? AND ts>? ORDER BY ts LIMIT ?",
            (uid, since, limit)).fetchall()
    return [json.loads(r["payload"]) for r in rows]
