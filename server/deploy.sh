#!/usr/bin/env bash
# 在 Azure Ubuntu VM 上一键部署（root 或 sudo 执行）
set -euo pipefail
echo "== 1/4 安装依赖"
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git curl ufw

echo "== 2/4 防火墙（只开 22/80/443）"
ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp
yes | ufw enable || true

echo "== 3/4 起服务（需要先写好 .env）"
cd "$(dirname "$0")"
[ -f .env ] || { echo "缺少 .env：请先写 UPSTREAM_KEY 与 SQA_JWT_SECRET"; exit 1; }
docker compose up -d --build

echo "== 4/4 自检"
sleep 3
curl -s http://127.0.0.1:8080/api/health || echo "健康检查失败：看 docker compose logs sqa"
echo
echo "下一步：装 caddy 并把域名解析到本机 IP（见 deploy-azure.md 第 3、4 节）"
