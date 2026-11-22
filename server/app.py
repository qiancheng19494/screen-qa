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

import store

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://api.deepseek.com").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
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
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
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
        if path == "/api/health":
            return self._json({"ok": True, "mock": MOCK, "upstream": UPSTREAM_BASE})
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
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
        return self._json({"answer": (ans.group(1).strip() if ans else content[:400]),
                           "fill": (fill.group(1).strip() if fill else ""),
                           "reasoning": content,
                           "usage": usage, "cost": round(_cost(pt, ct), 6)})

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()
    _secret()
    store.init()
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
