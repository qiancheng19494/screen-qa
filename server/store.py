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
  used_tokens INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active'
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


def _migrate(con: sqlite3.Connection) -> None:
    """老库增量升级：users.status（active / disabled）。"""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    if "status" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        _migrate(con)


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
        row = con.execute("SELECT id, salt, pw_hash, status FROM users WHERE name=?",
                          ((name or "").strip(),)).fetchone()
        if not row:
            return None, "用户名或密码不对"
        if not secrets.compare_digest(row["pw_hash"], _hash(pw or "", row["salt"])):
            return None, "用户名或密码不对"
        if row["status"] == "disabled":
            return None, "账号已被停用，请联系管理员"
        token = secrets.token_urlsafe(32)
        con.execute("INSERT INTO tokens(token, user_id, created_at) VALUES(?,?,?)",
                    (token, int(row["id"]), time.time()))
    return token, ""


def user_by_token(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    with connect() as con:
        return con.execute(
            "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token=? AND u.status='active'",
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


def charge_usage(uid: int, model: str, pt: int, ct: int, cost: float) -> None:
    """记录一次真实用量，并把 token 数计入当天的按天限额。

    与 add_usage 的区别：同时累加 users.used_tokens，使 take_quota()
    的剩余额度（以及 /api/me 的今日用量）真实生效。
    """
    today = time.strftime("%Y-%m-%d")
    tokens = max(0, int(pt) + int(ct))
    with connect() as con:
        con.execute("INSERT INTO usage(user_id, ts, model, pt, ct, cost) VALUES(?,?,?,?,?,?)",
                    (uid, time.time(), model, pt, ct, cost))
        row = con.execute("SELECT used_date, used_tokens FROM users WHERE id=?", (uid,)).fetchone()
        used = int(row["used_tokens"]) if row and row["used_date"] == today else 0
        con.execute("UPDATE users SET used_date=?, used_tokens=? WHERE id=?",
                    (today, used + tokens, uid))


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


# ---------- 管理后台 ----------

def admin_overview() -> dict:
    today = time.strftime("%Y-%m-%d")
    now = time.time()
    with connect() as con:
        users = con.execute("SELECT COUNT(*) n, "
                            "COALESCE(SUM(status='disabled'),0) d FROM users").fetchone()
        new_today = con.execute(
            "SELECT COUNT(*) n FROM users "
            "WHERE date(created_at,'unixepoch','localtime')=?", (today,)).fetchone()
        tokens = con.execute("SELECT COUNT(*) n FROM tokens t JOIN users u ON u.id=t.user_id "
                             "WHERE u.status='active'").fetchone()
        day = con.execute("SELECT COUNT(*) n, COALESCE(SUM(pt+ct),0) t, "
                          "COALESCE(SUM(cost),0) c FROM usage "
                          "WHERE date(ts,'unixepoch','localtime')=?", (today,)).fetchone()
        total = con.execute("SELECT COUNT(*) n, COALESCE(SUM(pt+ct),0) t, "
                            "COALESCE(SUM(cost),0) c FROM usage").fetchone()
        records = con.execute("SELECT COUNT(*) n FROM records").fetchone()
        active7 = con.execute("SELECT COUNT(DISTINCT user_id) n FROM usage WHERE ts>=?",
                              (now - 7 * 86400,)).fetchone()
    return {
        "users": int(users["n"]), "disabled": int(users["d"]),
        "new_today": int(new_today["n"]), "tokens": int(tokens["n"]),
        "active_7d": int(active7["n"]), "records": int(records["n"]),
        "today": {"n": int(day["n"]), "tokens": int(day["t"] or 0), "cost": round(float(day["c"] or 0), 6)},
        "total": {"n": int(total["n"]), "tokens": int(total["t"] or 0),
                  "cost": round(float(total["c"] or 0), 6)},
    }


def admin_users(q: str = "", limit: int = 100, offset: int = 0) -> dict:
    today = time.strftime("%Y-%m-%d")
    q = (q or "").strip()
    like = f"%{q}%"
    where = "u.name LIKE ?" if q else "1=1"
    args: list = [like] if q else []
    with connect() as con:
        total = con.execute(f"SELECT COUNT(*) n FROM users u WHERE {where}", args).fetchone()
        rows = con.execute(
            "SELECT u.id, u.name, u.created_at, u.quota_day, u.used_date, u.used_tokens, u.status,"
            " (SELECT COUNT(*) FROM tokens t WHERE t.user_id=u.id) tokens,"
            " (SELECT COUNT(*) FROM records r WHERE r.user_id=u.id) records,"
            " (SELECT MAX(ts) FROM usage g WHERE g.user_id=u.id) last_used,"
            " (SELECT COALESCE(SUM(pt+ct),0) FROM usage g WHERE g.user_id=u.id) tokens_total"
            f" FROM users u WHERE {where} ORDER BY u.id DESC LIMIT ? OFFSET ?",
            args + [max(1, min(500, int(limit))), max(0, int(offset))]).fetchall()
    items = []
    for r in rows:
        used = int(r["used_tokens"]) if r["used_date"] == today else 0
        items.append({
            "name": r["name"], "created_at": r["created_at"], "status": r["status"],
            "quota_day": int(r["quota_day"]), "used_today": used,
            "tokens": int(r["tokens"]), "records": int(r["records"]),
            "tokens_total": int(r["tokens_total"] or 0),
            "last_used": r["last_used"],
        })
    return {"total": int(total["n"]), "items": items}


def _user_by_name(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return con.execute("SELECT id, name, status FROM users WHERE name=?",
                       ((name or "").strip(),)).fetchone()


def admin_set_quota(name: str, quota_day: int) -> str:
    quota_day = int(quota_day)
    if not 0 <= quota_day <= 10_000_000:
        return "额度需在 0 ~ 10000000 之间"
    with connect() as con:
        if not _user_by_name(con, name):
            return "没有这个账号"
        con.execute("UPDATE users SET quota_day=? WHERE name=?", (quota_day, name.strip()))
    return ""


def admin_set_status(name: str, status: str) -> str:
    if status not in ("active", "disabled"):
        return "status 只能是 active 或 disabled"
    with connect() as con:
        row = _user_by_name(con, name)
        if not row:
            return "没有这个账号"
        con.execute("UPDATE users SET status=? WHERE id=?", (status, int(row["id"])))
        if status == "disabled":
            con.execute("DELETE FROM tokens WHERE user_id=?", (int(row["id"]),))
    return ""


def admin_revoke(name: str) -> str:
    with connect() as con:
        row = _user_by_name(con, name)
        if not row:
            return "没有这个账号"
        con.execute("DELETE FROM tokens WHERE user_id=?", (int(row["id"]),))
    return ""


def admin_export_rows() -> list[dict]:
    today = time.strftime("%Y-%m-%d")
    with connect() as con:
        rows = con.execute(
            "SELECT u.name, u.created_at, u.status, u.quota_day, u.used_date, u.used_tokens,"
            " (SELECT COUNT(*) FROM tokens t WHERE t.user_id=u.id) tokens,"
            " (SELECT COUNT(*) FROM records r WHERE r.user_id=u.id) records,"
            " (SELECT COALESCE(SUM(pt+ct),0) FROM usage g WHERE g.user_id=u.id) tokens_total,"
            " (SELECT COALESCE(SUM(cost),0) FROM usage g WHERE g.user_id=u.id) cost_total,"
            " (SELECT MAX(ts) FROM usage g WHERE g.user_id=u.id) last_used"
            " FROM users u ORDER BY u.id DESC").fetchall()
    out = []
    for r in rows:
        used = int(r["used_tokens"]) if r["used_date"] == today else 0
        out.append({
            "name": r["name"],
            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
            "status": r["status"], "quota_day": int(r["quota_day"]), "used_today": used,
            "tokens_total": int(r["tokens_total"] or 0), "cost_total": round(float(r["cost_total"] or 0), 6),
            "records": int(r["records"]), "tokens": int(r["tokens"]),
            "last_used": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_used"])) if r["last_used"] else "",
        })
    return out
