# 部署指南（1C1G 起步）

## 0. 这台服务器负责什么

| 云端能做的 | 云端做不到的 |
|---|---|
| 保管模型 Key（客户端不用填） | 截你的屏（只能在你电脑上） |
| 账号注册/登录 | 保证你的网络到它一直通 |
| 作答记录 / 错题本同步 | —— |
| 网页版：粘贴文字或上传图片解题 | —— |

## 1. 直接跑（不装 Docker）

```bash
# 服务器上：Python 3.10+
export UPSTREAM_KEY=sk-你的DeepSeekKey
export SQA_JWT_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
cd server && python3 app.py --port 8080
# 浏览器打开 http://服务器IP:8080
```

数据落在 `server/sqa.db`（SQLite）。备份就复制这个文件。

## 2. Docker（推荐）

```bash
cd server
echo "UPSTREAM_KEY=sk-你的Key" > .env
echo "SQA_JWT_SECRET=$(openssl rand -hex 32)" >> .env
docker compose up -d --build
curl http://127.0.0.1:8080/api/health
```

## 3. HTTPS 与域名（重要）

- 国外 VPS：用 caddy 一行搞定 `caddy reverse-proxy --from api.你的域名 --to 127.0.0.1:8080`
- **国内服务器**：80/443 必须先备案；没备案就先用 `http://IP:8080`（桌面端能用，网页版会被浏览器提示不安全）
- 客户端里填 `https://api.你的域名`（不带 `/api/proxy`，程序会自己拼）

## 4. 配额与安全

- `users.quota_day` 默认每人每天 30 万 token（约 ¥1）。改数据库或写个小接口都行。
- 服务器**不保存题目正文**，只记 `usage`（时间/模型/token/费用）。想连 usage 也不记，注释掉 `store.add_usage`。
- 令牌是随机 32 字节，存 `tokens` 表；要踢人直接删该行。
- 别把 `UPSTREAM_KEY` 写进镜像或前端；它只在服务器进程的环境变量里。

## 5. 网页版要"拍照解题"怎么办

Windows OCR 只能在 Windows 上用。Linux 服务器需要装 OCR：

```bash
pip install rapidocr-onnxruntime      # 约 100–250 MB
```

装好后 `/api/solve_image` 自动可用（先 OCR 再解题）；没装时该接口返回 501 并提示这句话。

## 6. 桌面端怎么接

1. 打开程序 → ⚙ → 「云端」→ 填服务器地址 → 登录/注册（令牌自动填）
2. 点「开启代理」：之后所有模型调用走你的服务器，本机 Key 不再参与
3. 「同步记录」把 `records.jsonl` 增量推到云端；想关就点「关闭代理」

命令行也行：`python tools/sync_cloud.py --since 0`
