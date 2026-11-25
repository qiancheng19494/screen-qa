"""屏幕问答云端：模型代理 + 账号/同步 + 网页版解题（纯标准库，单进程可跑 1C1G VPS）。

启动：
    set SQA_JWT_SECRET=随便一串      # 可选，不设会自动生成并落盘
    set UPSTREAM_BASE=https://api.deepseek.com
    set UPSTREAM_KEY=sk-xxxx         # 服务器持有的 Key，客户端不再需要
    python app.py --port 8080

接口：
    POST /api/register {name,password}       -> {token,user}
    POST /api/login    {name,password}       -> {token,user}
    GET  /api/me                             -> {user,stats}
    POST /v1/chat/completions                -> 透传上游（Bearer 用本服务的令牌）
    POST /api/sync/push {records:[],errors:[]} -> {saved}
    GET  /api/sync/pull?since=               -> {records:[],errors:[]}
    POST /api/solve {text, model?}           -> {answer,fill,reasoning,usage}
    POST /api/solve_image {image_b64}        -> 需要安装 rapidocr-onnxruntime
    GET  /                                   -> 网页版
"""
from __future__ import annotations
from collections import defaultdict, deque
import hmac

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import licensing
except Exception:  # noqa: BLE001  容器里若漏打包 licensing.py，服务仍要能起来
    licensing = None
import store

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://api.deepseek.com").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
_HITS: dict = defaultdict(deque)
MOCK = os.environ.get("SQA_MOCK", "") not in ("", "0")
SECRET_FILE = ROOT / "secret.key"
MAX_BODY = 8 * 1024 * 1024

SYSTEM = (
    "你是严谨的做题助手。先读懂题目，再一步步推理，最后给出可直接填入答题框的答案。\n"
    "输出格式固定为：\n## 推理\n（简明步骤）\n## 最终答案\n（题目的完整答案）\n"
    "## 填入答案\n（只写要敲进输入框的内容；分数写 3/5；多空按顺序用逗号分隔；选择题写字母）"
)

_FILL_RE = re.compile(r"##\s*填入答案\s*\n+(.+?)(?:\n\s*##|$)", re.S)
_ANS_RE = re.compile(r"##\s*最终答案\s*\n+(.+?)(?:\n\s*##|$)", re.S)


def _secret() -> str:
    env = os.environ.get("SQA_JWT_SECRET")
    if env:
        return env
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    s = secrets.token_urlsafe(32)
    SECRET_FILE.write_text(s, encoding="utf-8")
    return s


def _cost(pt: int, ct: int) -> float:
    return (pt or 0) / 1e6 * 2.0 + (ct or 0) / 1e6 * 8.0


def upstream_chat(payload: dict, stream_cb=None) -> dict:
    """调用上游；stream_cb 有值时走流式并把增量回调出去。"""
    if MOCK:
        text = "## 推理\n（测试模式，未真调用模型）\n## 最终答案\nx = 6\n## 填入答案\n6"
        if stream_cb:
            for ch in text:
                stream_cb(ch)
        return {"content": text, "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
    body = dict(payload)
    body["stream"] = bool(stream_cb)
    req = urllib.request.Request(
        UPSTREAM_BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {UPSTREAM_KEY}"},
        method="POST")
    ctx = ssl.create_default_context()
    timeout = 180 if stream_cb else 120
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        if not stream_cb:
            data = json.loads(resp.read().decode("utf-8"))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            return {"content": msg.get("content") or "", "usage": data.get("usage") or {}}
        buf, usage = [], {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except Exception:  # noqa: BLE001
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if delta:
                buf.append(delta)
                stream_cb(delta)
        return {"content": "".join(buf), "usage": usage}


class Handler(BaseHTTPRequestHandler):
    server_version = "ScreenQA/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def _json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    # ---------- 限流 ----------
    def _ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or ""
        return (xff.split(",")[0].strip() or self.client_address[0])[:64]

    def _limited(self, bucket: str, limit: int, window: float) -> bool:
        now = time.time()
        key = (bucket, self._ip())
        q = _HITS[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            retry = int(window - (now - q[0])) + 1
            body = json.dumps({"error": "too many requests, retry in %ds" % retry}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", str(retry))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        q.append(now)
        return False

    def _token(self) -> str:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def _user(self):
        return store.user_by_token(self._token())

    def log_message(self, fmt: str, *args) -> None:  # 只记方法/路径/状态，不记正文
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 路由 ----------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json({"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if self._limited("get", 240, 60.0):
            return
        if path in ("/favicon.ico", "/manifest.webmanifest", "/sw.js", "/robots.txt",
                    "/sitemap.xml", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            return self._static(path.lstrip("/"))
        if path == "/api/stats":
            u = self._user()
            if not u:
                return self._json({"error": "\u672a\u767b\u5f55"}, 401)
            st = store.stats(int(u["id"]))
            tot, tod = st.get("total", {}), st.get("today", {})
            return self._json({"stats": st,
                               "summary": {"total_n": tot.get("n", 0), "total_cost": tot.get("c", 0),
                                           "today_n": tod.get("n", 0), "today_cost": tod.get("c", 0)},
                               "daily": store.daily_usage(int(u["id"]), 30)})
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/errors":
            u = self._user()
            if not u:
                return self._json({"error": "未登录"}, 401)
            q = parse_qs(urlparse(self.path).query)
            limit = max(1, min(200, int((q.get("limit") or ["50"])[0] or 50)))
            rows = store.pull_records(int(u["id"]), "errors", 0.0, limit)
            import json as _json
            items = []
            for r in rows:
                try:
                    items.append(_json.loads(r["payload"]))
                except Exception:  # noqa: BLE001
                    continue
            items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
            return self._json({"items": items[:limit], "count": len(items)})
        if path == "/api/history":
            u = self._user()
            if not u:
                return self._json({"error": "未登录"}, 401)
            q = parse_qs(urlparse(self.path).query)
            limit = max(1, min(100, int((q.get("limit") or ["30"])[0] or 30)))
            rows = store.pull_records(int(u["id"]), "records", 0.0, limit)
            items = []
            for r in rows:
                try:
                    import json as _json
                    items.append(_json.loads(r["payload"]))
                except Exception:  # noqa: BLE001
                    continue
            items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
            return self._json({"items": items[:limit], "count": len(items)})
        if path == "/api/me":
            u = self._user()
            if not u:
                return self._json({"error": "未登录"}, 401)
            return self._json({"user": {"name": u["name"], "quota_day": u["quota_day"]},
                               "stats": store.stats(int(u["id"]))})
        if path == "/api/sync/pull":
            u = self._user()
            if not u:
                return self._json({"error": "未登录"}, 401)
            since = float(parse_qs(urlparse(self.path).query).get("since", ["0"])[0] or 0)
            return self._json({"records": store.pull_records(int(u["id"]), "records", since),
                               "errors": store.pull_records(int(u["id"]), "errors", since)})
        if path == "/api/admin/licenses":
            if not _admin_ok(self.headers.get("X-Admin-Token", "")):
                return self._json({"error": "forbidden"}, 403)
            if licensing is None:
                return self._json({"error": "授权模块未部署"}, 503)
            return self._json({"licenses": licensing.list_licenses(200)})
        if path == "/api/health":
            return self._json({"ok": True, "mock": MOCK, "upstream": UPSTREAM_BASE})
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        limit, bucket = (12, "auth") if path in ("/api/login", "/api/register") else (60, "post")
        if self._limited(bucket, limit, 60.0):
            return
        try:
            if path == "/api/register":
                b = self._body()
                token, err = store.register(b.get("name", ""), b.get("password", ""))
                return self._json({"token": token, "user": {"name": b.get("name", "")}},
                                  200) if token else self._json({"error": err}, 400)
            if path == "/api/login":
                b = self._body()
                token, err = store.login(b.get("name", ""), b.get("password", ""))
                return self._json({"token": token, "user": {"name": b.get("name", "")}},
                                  200) if token else self._json({"error": err}, 401)
            if path in ("/v1/chat/completions", "/api/proxy/chat/completions"):
                return self._proxy()
            if path == "/api/sync/push":
                u = self._user()
                if not u:
                    return self._json({"error": "未登录"}, 401)
                b = self._body()
                n1 = store.push_records(int(u["id"]), "records", b.get("records") or [])
                n2 = store.push_records(int(u["id"]), "errors", b.get("errors") or [])
                return self._json({"saved": n1 + n2})
            if path == "/api/license/activate":
                b = self._body()
                if licensing is None:
                    return self._json({"ok": False, "error": "授权模块未部署"}, 503)
                return self._json(licensing.activate(b.get("key", ""), b.get("machine", ""), self._ip()))
            if path == "/api/license/check":
                b = self._body()
                if licensing is None:
                    return self._json({"ok": False, "error": "授权模块未部署"}, 503)
                return self._json(licensing.check(b.get("key", ""), b.get("machine", ""), self._ip()))
            if path == "/api/admin/newkey":
                if not _admin_ok(self.headers.get("X-Admin-Token", "")):
                    return self._json({"error": "forbidden"}, 403)
                b = self._body()
                days = int(b.get("days", 365) or 365)
                if licensing is None:
                    return self._json({"error": "授权模块未部署"}, 503)
                k = licensing.new_key(days=days, note=str(b.get("note", ""))[:120],
                                      machine_limit=int(b.get("machines", 1) or 1))
                return self._json({"key": k, "days": days})
            if path == "/api/error":
                u = self._user()
                if not u:
                    return self._json({"error": "未登录"}, 401)
                b = self._body()
                import hashlib as _hl
                import json as _json
                import time as _t
                rec = {"text": (b.get("text") or "")[:500],
                       "answer": (b.get("answer") or "")[:300],
                       "fill": (b.get("fill") or "")[:200],
                       "note": (b.get("note") or "")[:200], "ts": _t.time()}
                store.push_records(int(u["id"]), "errors", [{
                    "uid": _hl.sha1((str(_t.time()) + rec["text"]).encode()).hexdigest()[:16],
                    "ts": rec["ts"], "payload": _json.dumps(rec, ensure_ascii=False)}])
                return self._json({"saved": 1})
            if path == "/api/solve":
                return self._solve()
            if path == "/api/solve_image":
                return self._solve_image()
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "not found"}, 404)

    # ---------- 具体实现 ----------
    def _static(self, name: str) -> None:
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            return self._json({"error": "not found"}, 404)
        data = f.read_bytes()
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self) -> None:
        u = self._user()
        if not u:
            return self._json({"error": {"message": "未登录或令牌无效"}}, 401)
        b = self._body()
        want_stream = bool(b.get("stream"))
        est = 4000
        ok, left = store.take_quota(u, 0)
        if int(u["quota_day"]) <= 0:
            return self._json({"error": {"message": "该账号未开额度"}}, 402)
        if left <= 0:
            return self._json({"error": {"message": "今日额度用完了"}}, 429)
        if not want_stream:
            try:
                out = upstream_chat(b)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                return self._json({"error": {"message": detail}}, exc.code)
            usage = out.get("usage") or {}
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            store.add_usage(int(u["id"]), str(b.get("model") or ""), pt, ct, _cost(pt, ct))
            return self._json({"id": "sqa", "choices": [{"index": 0, "message":
                               {"role": "assistant", "content": out.get("content") or ""},
                               "finish_reason": "stop"}], "usage": usage})
        # 流式：直接把上游 SSE 转给客户端
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def cb(delta: str) -> None:
            frame = {"choices": [{"index": 0, "delta": {"content": delta}}]}
            self.wfile.write(b"data: " + json.dumps(frame, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.flush()

        try:
            out = upstream_chat(b, stream_cb=cb)
        except Exception as exc:  # noqa: BLE001
            err = {"error": {"message": f"{type(exc).__name__}: {exc}"}}
            self.wfile.write(b"data: " + json.dumps(err, ensure_ascii=False).encode() + b"\n\n")
        else:
            usage = out.get("usage") or {}
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            store.add_usage(int(u["id"]), str(b.get("model") or ""), pt, ct, _cost(pt, ct))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        try:
            self.wfile.write(b"")  # 结束块
        except Exception:  # noqa: BLE001
            pass
        self.close_connection = True

    def _solve(self) -> None:
        b = self._body()
        text = (b.get("text") or "").strip()
        if len(text) < 4:
            return self._json({"error": "题目太短"}, 400)
        u = self._user()
        if u:
            ok, left = store.take_quota(u, 0)
            if left <= 0:
                return self._json({"error": "今日额度用完了"}, 429)
        model = b.get("model") or "deepseek-chat"
        if b.get("multi"):
            return self._solve_multi(text, model, u)
        if b.get("stream"):
            return self._solve_stream(text, model, u)
        try:
            out = upstream_chat({"model": model, "temperature": 0.2, "max_tokens": 1200,
                                 "messages": [{"role": "system", "content": SYSTEM},
                                              {"role": "user", "content": text[:6000]}]})
        except urllib.error.HTTPError as exc:
            return self._json({"error": exc.read().decode("utf-8", "replace")[:300]}, exc.code)
        content = out.get("content") or ""
        fill = _FILL_RE.search(content)
        ans = _ANS_RE.search(content)
        usage = out.get("usage") or {}
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        if u:
            store.add_usage(int(u["id"]), model, pt, ct, _cost(pt, ct))
            try:
                import hashlib as _hl
                import json as _json
                import time as _t
                rec = {"text": text[:500], "answer": (ans.group(1).strip() if ans else "")[:300],
                       "fill": (fill.group(1).strip() if fill else "")[:200],
                       "model": model, "pt": pt, "ct": ct, "ts": _t.time()}
                store.push_records(int(u["id"]), "records", [{
                    "uid": _hl.sha1((str(_t.time()) + text).encode()).hexdigest()[:16],
                    "ts": rec["ts"], "payload": _json.dumps(rec, ensure_ascii=False)}])
            except Exception:  # noqa: BLE001
                pass
        return self._json({"answer": (ans.group(1).strip() if ans else content[:400]),
                           "fill": (fill.group(1).strip() if fill else ""),
                           "reasoning": content,
                           "usage": usage, "cost": round(_cost(pt, ct), 6)})

    def _parse(self, content: str):
        fill = _FILL_RE.search(content)
        ans = _ANS_RE.search(content)
        return (ans.group(1).strip() if ans else content[:400],
                fill.group(1).strip() if fill else "")

    def _save_record(self, u, text, answer, fill, model, pt, ct) -> None:
        if not u:
            return
        try:
            import hashlib as _hl
            import json as _json
            import time as _t
            rec = {"text": text[:500], "answer": answer[:300], "fill": fill[:200],
                   "model": model, "pt": pt, "ct": ct, "ts": _t.time()}
            store.push_records(int(u["id"]), "records", [{
                "uid": _hl.sha1((str(_t.time()) + text).encode()).hexdigest()[:16],
                "ts": rec["ts"], "payload": _json.dumps(rec, ensure_ascii=False)}])
        except Exception:  # noqa: BLE001
            pass

    def _solve_stream(self, text: str, model: str, u) -> None:
        """SSE 流式作答：先发心跳，再尝试真流式；首帧超时则回退非流式并伪流式下发。"""
        import json as _json
        import threading
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def raw_write(payload: dict) -> bool:
            try:
                self.wfile.write(("data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                return True
            except Exception:  # noqa: BLE001
                return False

        # 心跳：让浏览器/Caddy 立刻见到字节，避免"连上了但一直空"
        try:
            self.wfile.write(b": ok\n\n")
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            return

        state = {"first": 0.0, "abandon": False}
        buf = []

        def cb(ch: str) -> None:
            if state["abandon"]:
                return
            state["first"] = state["first"] or time.time()
            buf.append(ch)
            raw_write({"delta": ch})

        payload = {"model": model, "temperature": 0.2, "max_tokens": 1200,
                   "messages": [{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": text[:6000]}]}
        box: dict = {}

        def work() -> None:
            try:
                box["out"] = upstream_chat(payload, stream_cb=cb)
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(25)
        content, usage = "", {}
        if th.is_alive() or "err" in box or not state["first"]:
            # 回退：非流式（这条路径在 mock 与真实上游都验证过能返回）
            state["abandon"] = True
            try:
                out = upstream_chat(dict(payload))
            except Exception as exc:  # noqa: BLE001
                raw_write({"error": f"{type(exc).__name__}: {exc}"})
                return
            content = out.get("content") or ""
            usage = out.get("usage") or {}
            step = 24
            for i in range(0, len(content), step):
                raw_write({"delta": content[i:i + step]})
                time.sleep(0.015)
        else:
            out = box.get("out") or {}
            content = out.get("content") or "".join(buf)
            usage = out.get("usage") or {}
        answer, fill = self._parse(content)
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        if u:
            try:
                store.add_usage(int(u["id"]), model, pt, ct, _cost(pt, ct))
                self._save_record(u, text, answer, fill, model, pt, ct)
            except Exception:  # noqa: BLE001
                pass
        raw_write({"done": True, "answer": answer, "fill": fill, "content": content,
                   "usage": usage, "cost": _cost(pt, ct) if (pt or ct) else 0})

    def _solve_multi(self, text: str, model: str, u) -> None:
        import re as _re
        parts = _re.split(r"(?m)^\s*\d{1,2}\s*[.)\u3001]\s+|\n{2,}", text)
        qs = [p.strip() for p in parts if p and p.strip() and len(p.strip()) >= 4]
        qs = [q for q in qs if not q.isdigit()][:8]
        if len(qs) < 2:
            qs = [text.strip()]
        items = []
        for q in qs:
            try:
                out = upstream_chat({"model": model, "temperature": 0.2, "max_tokens": 1200,
                                     "messages": [{"role": "system", "content": SYSTEM},
                                                  {"role": "user", "content": q[:6000]}]})
            except Exception as exc:  # noqa: BLE001
                items.append({"text": q, "error": f"{type(exc).__name__}: {exc}"})
                continue
            content = out.get("content") or ""
            answer, fill = self._parse(content)
            usage = out.get("usage") or {}
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            if u:
                store.add_usage(int(u["id"]), model, pt, ct, _cost(pt, ct))
                self._save_record(u, q, answer, fill, model, pt, ct)
            items.append({"text": q[:300], "answer": answer, "fill": fill,
                          "reasoning": content[:1500], "cost": round(_cost(pt, ct), 6)})
        return self._json({"items": items, "count": len(items)})

    def _solve_image(self) -> None:
        b = self._body()
        raw = base64.b64decode((b.get("image_b64") or "").split(",")[-1] or b"")
        if len(raw) < 100:
            return self._json({"error": "图片为空"}, 400)
        try:
            from rapidocr_onnxruntime import RapidOCR  # 可选依赖
        except Exception:  # noqa: BLE001
            return self._json({"error": "服务器没装 OCR 引擎：pip install rapidocr-onnxruntime"}, 501)
        tmp = ROOT / "_upload.png"
        tmp.write_bytes(raw)
        engine = RapidOCR()
        result, _ = engine(str(tmp))
        tmp.unlink(missing_ok=True)
        text = "\n".join(item[1] for item in (result or []))
        if not text.strip():
            return self._json({"error": "没识别到文字，换个清晰点的截图"}, 422)
        b["text"] = text
        self._body_backup = b
        return self._solve_text_direct(text, b.get("model") or "deepseek-chat")

    def _solve_text_direct(self, text: str, model: str) -> None:
        out = upstream_chat({"model": model, "temperature": 0.2, "max_tokens": 1200,
                             "messages": [{"role": "system", "content": SYSTEM},
                                          {"role": "user", "content": text[:6000]}]})
        content = out.get("content") or ""
        fill = _FILL_RE.search(content)
        ans = _ANS_RE.search(content)
        return self._json({"text": text,
                           "answer": (ans.group(1).strip() if ans else content[:400]),
                           "fill": (fill.group(1).strip() if fill else ""),
                           "reasoning": content})


ADMIN_TOKEN = os.environ.get("SQA_ADMIN_TOKEN", "")


def _admin_ok(token: str) -> bool:
    want = ADMIN_TOKEN or ""
    return bool(want) and hmac.compare_digest((token or "").strip(), want)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()
    _secret()
    store.init()
    if licensing is not None:
        licensing.init()
    if MOCK:
        print("[!] SQA_MOCK=1：不调用真实模型，返回固定答案")
    if not UPSTREAM_KEY and not MOCK:
        print("[!] 没设 UPSTREAM_KEY，代理会 401")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[+] http://{args.host}:{args.port}  upstream={UPSTREAM_BASE} mock={MOCK}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[.] bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
