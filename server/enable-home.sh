#!/usr/bin/env bash
# 把个人主页从 GitHub Pages 切到本机：前提是 wuyolo.com / www 的 A 记录已指向本机公网 IP
set -euo pipefail

IP="$(curl -fsS --max-time 8 https://api.ipify.org || true)"
[ -n "$IP" ] || { echo "拿不到本机公网 IP"; exit 1; }
ROOT_IP="$(getent hosts wuyolo.com | awk '{print $1; exit}' || true)"
WWW_IP="$(getent hosts www.wuyolo.com | awk '{print $1; exit}' || true)"

echo "本机公网 IP : $IP"
echo "wuyolo.com  : ${ROOT_IP:-<未解析>}"
echo "www         : ${WWW_IP:-<未解析>}"

if [ "$ROOT_IP" != "$IP" ]; then
  cat <<EOF

✗ 根域还没指向本机，先改 DNS：
   GoDaddy → DNS → 修改 A 记录
     @    → $IP
     www  → $IP
   （改完等 5-30 分钟，用 dig +short wuyolo.com 确认是本机 IP）

改好后重新运行：sudo bash $0
EOF
  exit 2
fi

mkdir -p /srv/home
if [ -f /srv/sqa/server/home/index.html ]; then
  cp /srv/sqa/server/home/index.html /srv/home/index.html
fi

cat > /etc/caddy/Caddyfile <<EOF
# 个人主页（静态）
wuyolo.com, www.wuyolo.com {
    root * /srv/home
    encode gzip
    file_server
    header /index.html Cache-Control "no-cache"
}

# 答题网页（反代到容器，关闭缓冲以支持流式）
q.wuyolo.com {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
EOF

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy || systemctl restart caddy
sleep 2
systemctl is-active caddy
echo
echo "✅ 已切到本机：https://wuyolo.com （证书由 Caddy 自动申请，首次约 10-30 秒）"
