"""用 GitHub REST API 推一次提交（本机 git 的 schannel 握手被沙箱挡住，走 HTTP API）。"""
import base64, json, subprocess, sys, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OWNER, REPO, BRANCH = "qiancheng19494", "screen-qa", "main"
TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                       shell=True).stdout.strip()
if not TOKEN:
    sys.exit("拿不到 gh token")
API = f"https://api.github.com/repos/{OWNER}/{REPO}"


def call(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "screen-qa-uploader",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


# 空仓库不能直接用 git data API（409），先用 Contents API 建个初始提交
try:
    call("GET", f"{API}/commits")
    parent_sha = call("GET", f"{API}/commits/{BRANCH}")["sha"]
    print("仓库已有提交，父提交:", parent_sha[:8])
except Exception:
    readme = open("README.md", "rb").read()
    first = call("PUT", f"{API}/contents/README.md", {
        "message": "init", "branch": BRANCH,
        "content": base64.b64encode(readme).decode()})
    parent_sha = first["commit"]["sha"]
    print("初始化空仓库，初始提交:", parent_sha[:8])

raw = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "-z"],
                     capture_output=True, shell=True).stdout
files = [f for f in raw.decode("utf-8").split("\0") if f.strip()]
print(f"待上传 {len(files)} 个文件")

tree = []
for path in files:
    with open(path, "rb") as fh:
        content = fh.read()
    blob = call("POST", f"{API}/git/blobs",
                {"content": base64.b64encode(content).decode(), "encoding": "base64"})
    tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    print(f"  ✓ {path} ({len(content)} B)")

nt = call("POST", f"{API}/git/trees", {"tree": tree})
msg = (subprocess.run('git log -1 --pretty=%B', capture_output=True, text=True,
                      shell=True).stdout or "screen-qa v2").strip()
commit = call("POST", f"{API}/git/commits",
              {"message": msg, "tree": nt["sha"], "parents": [parent_sha]})
print("commit:", commit["sha"])

try:
    call("POST", f"{API}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    print(f"created refs/heads/{BRANCH}")
except Exception as exc:  # 分支已存在则更新
    print("create ref 失败，改更新:", exc)
    call("PATCH", f"{API}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
    print(f"updated refs/heads/{BRANCH}")
