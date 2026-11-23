# 把 wuyolo.com 部署到 Azure 服务器（学生订阅）

目标：`https://wuyolo.com` 打开网页版（答题网页 + 模型代理），后台是我们仓库里的 `server/`。

## 0. 现在的事实（我查过了）

- `wuyolo.com` 的 NS 是 `ns11/ns12.domaincontrol.com` → **域名在 GoDaddy，DNS 也在 GoDaddy**
- 现在 A 记录指向 `160.153.0.182` → 这是 **GoDaddy 的停放/建站页**，也就是说域名现在没有指向你自己的服务器
- 所以要做的事只有两件：**① 弄一台公网服务器 ② 把 GoDaddy 的 A 记录改到那台服务器**

## 1. Azure：开一台最小的 Linux VM（约 10 分钟）

Azure for Students 有 $100 额度 + 免费 B1s（1 核 1G，750 小时/月），跑我们这套足够。

1. 打开 https://portal.azure.com → 搜 **Virtual machines** → **Create**
2. 关键项：
   - Resource group：新建，如 `wuyolo-rg`
   - Region：**Southeast Asia（新加坡）** 或 **East Asia（香港）** —— 国内访问较快；不要选"China"区域（需要备案）
   - Image：**Ubuntu Server 22.04 LTS**
   - Size：**Standard_B1s**（1 vCPU / 1 GiB）
   - Authentication：**SSH public key**（生成或用现有的）
   - Inbound ports：勾 **SSH(22)**、**HTTP(80)**、**HTTPS(443)**
3. 创建后记下 **Public IP address**（例如 `20.x.x.x`）

验证：本机执行 `ssh azureuser@<公网IP>` 能进去即可。

## 2. 服务器上装环境 + 起服务（约 3 分钟）

```bash
# 1) 装 docker
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git ufw
# 2) 拉代码（或把本地 server/ 目录 scp 上去）
git clone https://github.com/qiancheng19494/screen-qa.git && cd screen-qa/server
# 3) 写环境变量
cat > .env <<'EOF'
UPSTREAM_KEY=sk-你的DeepSeekKey
SQA_JWT_SECRET=换成一串32位随机字符
EOF
# 4) 起容器（只监听本机 8080）
sudo docker compose up -d --build
# 5) 自检
curl -s http://127.0.0.1:8080/api/health
```

## 3. HTTPS 反代（Caddy 自动证书）

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy
```

写 `/etc/caddy/Caddyfile`：

```
wuyolo.com, www.wuyolo.com {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo systemctl restart caddy && sudo systemctl status caddy --no-pager
```

Caddy 会自动申请 Let's Encrypt 证书（**前提：域名已经解析到这台机器，且 80/443 放行**）。

## 4. GoDaddy：把域名指过来（约 3 分钟）

1. 登录 GoDaddy → **My Products** → `wuyolo.com` 右侧 **DNS**
2. 找 `A` 记录 `@`（指向 `160.153.0.182` 那条）→ 改成 **你的 Azure 公网 IP**，TTL 600
3. 找 `CNAME` `www` → 指向 `wuyolo.com`（如果现在指向 GoDaddy 停放页，一并改）
4. 删除 GoDaddy 的"停放页/建站"相关记录（通常是与 A 记录同名的 forwarding 或 parking）
5. 等 1–10 分钟生效，本机验证：
   - `nslookup wuyolo.com` 应显示你的 Azure IP
   - 浏览器打开 `https://wuyolo.com`

## 5. 上线后自检清单

```bash
curl -I http://wuyolo.com          # 应 200 或 308
curl -I https://wuyolo.com         # 应 200，证书有效
curl -s https://wuyolo.com/api/health
```
网页上：注册 → 登录 → 文本解题一次 → 看是否扣额度（`/api/me` 里 quota 变化）。

## 6. 必须知道的几件事

- **备案**：服务器在境外（新加坡/香港）+ .com 域名，**不需要 ICP 备案**；但如果选 Azure 的 China 区域，就必须备案（个人备案约 1–3 周）。
- **国内访问速度**：新加坡/香港通常 80–200ms，可用；美国区域会明显慢。
- **安全**：只开 22/80/443；`ADMIN`/注册接口别开公开滥用（我们有每用户每日 30 万 token 配额）；`server/sqa.db` 是数据，定期备份。
- **别把 DeepSeek Key 放前端**：Key 只在服务器 `.env` 里，客户端走 `/api/proxy`。
- **桌面版可用它**：设置 →「云端」→ 填 `https://wuyolo.com` + 注册账号 → 开启代理，就不用本机存 Key 了。

## 7. 我可以代做的 vs 你要自己点的

| 事项 | 谁做 |
|---|---|
| 部署脚本、Caddy 配置、Dockerfile 调整、域名解析排查 | 我做（都已写好在仓库里） |
| Azure 创建 VM、拿公网 IP | 你（要你的 Azure 登录） |
| GoDaddy 改 DNS | 你（要你的 GoDaddy 登录） |
| 服务器上跑命令 | 你复制粘贴（或把 SSH 交给我所在的机器，但我不建议给凭据） |
| 上线后验证、排错、加功能 | 我做 |
