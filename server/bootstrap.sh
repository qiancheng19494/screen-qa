#!/usr/bin/env bash
# wuyolo.com 一键部署：个人主页（静态）+ q.wuyolo.com 答题页（Docker 反代）
# 用法（在服务器上，root 或 sudo）：
#   curl -fsSL https://raw.githubusercontent.com/qiancheng19494/screen-qa/main/server/bootstrap.sh | sudo bash -s -- <DeepSeekKey>
# 不传 Key 也能装，稍后编辑 /srv/sqa/server/.env 里的 UPSTREAM_KEY 再 docker compose up -d
set -euo pipefail
KEY="${1:-}"
REPO="https://github.com/qiancheng19494/screen-qa.git"
DIR="/srv/sqa"
HOME_DIR="/srv/home"

log() { printf "\n\033[1;36m== %s\033[0m\n" "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "请用 sudo 运行"; exit 1; fi

log "1/7 安装依赖（docker / caddy / git）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git caddy curl ca-certificates >/dev/null
systemctl enable --now docker

log "2/7 防火墙（有 ufw 才配）"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null 2>&1 || true
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  echo "ufw 已放行 22/80/443"
else
  echo "无 ufw，跳过"
fi

log "3/7 拉取代码到 $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q
else
  mkdir -p "$(dirname "$DIR")"
  git clone -q --depth 1 "$REPO" "$DIR"
fi

log "4/7 生成 .env"
ENVF="$DIR/server/.env"
if [ ! -f "$ENVF" ]; then
  {
    echo "UPSTREAM_KEY=${KEY:-sk-REPLACE_ME}"
    echo "SQA_JWT_SECRET=$(openssl rand -hex 16)"
  } > "$ENVF"
  chmod 600 "$ENVF"
  echo "已生成 $ENVF"
else
  echo "已存在，保留"
  if [ -n "$KEY" ]; then sed -i "s|^UPSTREAM_KEY=.*|UPSTREAM_KEY=$KEY|" "$ENVF" && echo "已更新 Key"; fi
fi

log "5/7 启动答题服务（容器，监听 127.0.0.1:8080）"
cd "$DIR/server"
docker compose up -d --build
sleep 3
curl -fsS http://127.0.0.1:8080/api/health && echo " <- 健康检查通过" || { echo "健康检查失败，看日志：docker compose logs --tail 50"; exit 1; }

log "6/7 部署个人主页到 $HOME_DIR"
mkdir -p "$HOME_DIR"
cp -f "$DIR/server/home/index.html" "$HOME_DIR/index.html"
chown -R www-data:www-data "$HOME_DIR" 2>/dev/null || true

log "7/7 配置 Caddy 并 reload"
cp -f "$DIR/server/Caddyfile.wuyolo" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl enable --now caddy >/dev/null 2>&1
systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 2
systemctl --no-pager --lines=0 status caddy | head -3 || true

log "完成"
echo "个人主页   : http://$(hostname -I | awk '{print $1}')/        （域名生效后 https://wuyolo.com）"
echo "答题页     : http://127.0.0.1:8080                        （域名生效后 https://q.wuyolo.com）"
echo
echo "后续常用命令："
echo "  看答题服务日志   cd $DIR/server && docker compose logs -f sqa"
echo "  看 Caddy 日志     journalctl -u caddy -f"
echo "  改主页            nano $HOME_DIR/index.html"
echo "  更新代码          cd $DIR && git pull && cd server && docker compose up -d --build"
