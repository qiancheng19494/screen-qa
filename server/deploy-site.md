# wuyolo.com 站点结构：个人主页 + 二级页（答题网页）

目标：

| 地址 | 内容 | 实现 |
|---|---|---|
| `https://wuyolo.com` | **你的个人主页** | Caddy 直接托管静态文件 `/srv/home` |
| `https://q.wuyolo.com` | **答题网页**（screen-qa 云端版） | Caddy 反代到 `127.0.0.1:8080`（Docker） |
| `https://www.wuyolo.com` | 301 跳到裸域 | Caddy redir |

> 为什么不放 `wuyolo.com/sqa`？因为答题网页的前端用的是绝对路径 `/api/solve`、`/api/login`，
> 挂在子路径下会 404。子域名方案不用改一行前端代码，而且证书各自独立。

## 1. DNS（GoDaddy，共 3 条）

在 GoDaddy → `wuyolo.com` → DNS 里：

| 类型 | 名称 | 值 | TTL |
|---|---|---|---|
| A | `@` | `<Azure 公网 IP>` | 600 |
| A | `q` | `<Azure 公网 IP>` | 600 |
| A | `www` | `<Azure 公网 IP>` | 600 |

- 把原来指向 `160.153.0.182`（GoDaddy 停放页）的记录**改成**上面的 IP，或者删掉对同名的旧记录。
- 想更省事：`q` 和 `www` 用 `CNAME` 指向 `wuyolo.com` 也行。
- 验证：`nslookup wuyolo.com`、`nslookup q.wuyolo.com` 都应返回你的 IP。

## 2. 服务器准备（一次性）

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git caddy ufw
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw --force enable
```

## 3. 部署个人主页（静态）

```bash
sudo mkdir -p /srv/home
# 从仓库拿模板（或自己写一份丢进去）
git clone https://github.com/qiancheng19494/screen-qa.git /tmp/sqa
sudo cp /tmp/sqa/server/home/index.html /srv/home/index.html
sudo chown -R www-data:www-data /srv/home
```
以后改主页：直接改 `/srv/home/index.html`（或把整个站点目录 rsync 上去），无需重启。

## 4. 部署答题网页（Docker）

```bash
cd /tmp/sqa/server
printf 'UPSTREAM_KEY=sk-你的DeepSeekKey\nSQA_JWT_SECRET=%s\n' "$(openssl rand -hex 16)" > .env
sudo docker compose up -d --build
curl -s http://127.0.0.1:8080/api/health     # 应返回 ok
```

## 5. 配置 Caddy（两个站点 + 自动 HTTPS）

```bash
sudo cp /tmp/sqa/server/Caddyfile.wuyolo /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo journalctl -u caddy -n 30 --no-pager     # 看证书是否签发成功
```

Caddy 会自动为 `wuyolo.com`、`q.wuyolo.com` 各申请一张 Let's Encrypt 证书（前提：DNS 已生效 + 80/443 放行）。

## 6. 上线自检

```bash
curl -I https://wuyolo.com            # 200，看你主页
curl -I https://q.wuyolo.com          # 200，看答题网页
curl -s https://q.wuyolo.com/api/health
```
浏览器：`https://q.wuyolo.com` 注册 → 登录 → 文本解题一次，确认有返回。

## 7. 桌面版接到云端（可选）

桌面程序 → ⚙ 设置 → 「云端」：服务器填 `https://q.wuyolo.com`，注册/登录后点「开启代理」，
之后模型调用经过你的服务器，本机不再需要 DeepSeek Key。

## 8. 维护

| 要做的事 | 命令 |
|---|---|
| 改主页 | 编辑 `/srv/home/index.html` |
| 更新答题程序 | `cd /srv/sqa/server && git pull && sudo docker compose up -d --build` |
| 看日志 | `sudo docker compose logs -f sqa` / `sudo journalctl -u caddy -f` |
| 备份数据 | `sudo cp /srv/sqa/server/data/sqa.db ~/sqa-$(date +%F).db` |
| 续期证书 | Caddy 自动，无需手动 |
