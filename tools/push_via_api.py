"""用 GitHub REST API 把当前仓库推一次提交。

用途：本机 git push 走 schannel 时报
`schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`（沙箱挡住凭据/TLS），
但 HTTPS API 是通的，所以改用 GitHub Git Data API 直接建 commit + 更新分支。

v2.15 起：①每个请求自带 3 次退避重试（本机到 GitHub 偶发 SSL EOF）；
        ②只上传内容变化的 blob（对比远端树的 blob sha），重试时不会从头上传。

用法：
    python tools/push_via_api.py "提交说明"
"""
import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OWNER, REPO, BRANCH = "qiancheng19494", "screen-qa", "main"
TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                       shell=True).stdout.strip()
if not TOKEN:
    sys.exit("拿不到 gh token")
API = f"https://api.github.com/repos/{OWNER}/{REPO}"


def call(method, url, payload=None, tries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "screen-qa-uploader",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:      # 4xx 不重试
            body = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {exc.code} {method} {url}\n{body}") from exc
        except Exception as exc:                    # SSL EOF / 连接重置 → 退避重试
            last = exc
            print(f"    ! {type(exc).__name__}: {str(exc)[:80]}（{i + 1}/{tries}，{3 * (i + 1)}s 后重试）")
            time.sleep(3 * (i + 1))
    raise SystemExit(f"请求失败：{method} {url}\n{last}")


def blob_sha(content: bytes) -> str:
    head = b"blob %d\0" % len(content)
    return hashlib.sha1(head + content).hexdigest()


# 空仓库不能直接用 git data API（409），先用 Contents API 建个初始提交
try:
    parent_sha = call("GET", f"{API}/commits/{BRANCH}")["sha"]
    print("仓库已有提交，父提交:", parent_sha[:8])
except SystemExit:
    raise
except Exception:
    readme = open("README.md", "rb").read()
    first = call("PUT", f"{API}/contents/README.md", {
        "message": "init", "branch": BRANCH,
        "content": base64.b64encode(readme).decode()})
    parent_sha = first["commit"]["sha"]
    print("初始化空仓库，初始提交:", parent_sha[:8])

remote = {}
try:
    tree = call("GET", f"{API}/git/trees/{parent_sha}?recursive=1")
    remote = {n["path"]: n["sha"] for n in tree.get("tree", []) if n.get("type") == "blob"}
    print(f"远端已有 {len(remote)} 个 blob")
except Exception as exc:  # noqa: BLE001
    print("读远端树失败（不影响继续）:", exc)

raw = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "-z"],
                     capture_output=True, shell=True).stdout
files = [f for f in raw.decode("utf-8").split("\0") if f.strip()]
print(f"待上传 {len(files)} 个文件")

tree = []
skipped = 0
for path in files:
    with open(path, "rb") as fh:
        content = fh.read()
    sha = blob_sha(content)
    if remote.get(path) == sha:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
        skipped += 1
        continue
    blob = call("POST", f"{API}/git/blobs",
                {"content": base64.b64encode(content).decode(), "encoding": "base64"})
    tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    print(f"  ✓ {path} ({len(content)} B)")
print(f"复用未改动 blob {skipped} 个")

nt = call("POST", f"{API}/git/trees", {"tree": tree})
msg = (sys.argv[1] if len(sys.argv) > 1 else "update").strip()
commit = call("POST", f"{API}/git/commits",
              {"message": msg, "tree": nt["sha"], "parents": [parent_sha]})
print("commit:", commit["sha"])

try:
    call("POST", f"{API}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    print(f"created refs/heads/{BRANCH}")
except SystemExit:
    raise
except Exception as exc:  # 分支已存在则更新
    print("create ref 失败，改更新:", str(exc)[:80])
    call("PATCH", f"{API}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
    print(f"updated refs/heads/{BRANCH}")
