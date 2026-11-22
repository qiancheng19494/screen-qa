"""云端客户端：登录/注册、记录同步、连通性测试（纯 urllib，不引依赖）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def _call(base: str, path: str, obj=None, token: str = "", timeout: int = 20):
    base = (base or "").rstrip("/")
    if not base:
        raise ValueError("还没填云端地址")
    url = base + path
    data = json.dumps(obj).encode("utf-8") if obj is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return {"error": (json.loads(body) or {}).get("error") or body[:200], "status": exc.code}
        except Exception:  # noqa: BLE001
            return {"error": body[:200], "status": exc.code}
    except urllib.error.URLError as exc:
        return {"error": f"连不上：{exc.reason}"}


def health(base: str) -> dict:
    return _call(base, "/api/health", timeout=8)


def login(base: str, name: str, pw: str, register: bool = False) -> dict:
    return _call(base, "/api/register" if register else "/api/login",
                 {"name": name, "password": pw})


def me(base: str, token: str) -> dict:
    return _call(base, "/api/me", token=token, timeout=10)


def push(base: str, token: str, records: list, errors: list) -> dict:
    return _call(base, "/api/sync/push", {"records": records, "errors": errors},
                 token=token, timeout=30)


def pull(base: str, token: str, since: float = 0.0) -> dict:
    return _call(base, f"/api/sync/pull?since={since}", token=token, timeout=30)


def prepare_records(path, last_ts: float = 0.0) -> list:
    """把 records.jsonl 里 ts 更新的记录转成带 uid 的列表。"""
    import hashlib
    import json as _json
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                ts = float(rec.get("ts") or 0)
                if ts <= last_ts:
                    continue
                key = rec.get("question") or rec.get("title") or ""
                rec.setdefault("ts", ts)
                rec["uid"] = hashlib.sha1((key + "|" + str(ts)).encode("utf-8")).hexdigest()[:16]
                out.append(rec)
    except FileNotFoundError:
        pass
    return out
