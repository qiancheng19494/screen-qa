#!/usr/bin/env bash
# 把个人主页从 GitHub Pages 切到本机。
#   bash enable-home.sh           只检查 DNS，指向本机才配置
#   bash enable-home.sh --force   跳过等待：立即配好 Caddy（证书会等 DNS 生效后自动签发）
set -euo pipefail

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

IP="$(curl -fsS --max-time 8 https://api.ipify.org || true)"
[ -n "$IP" ] || { echo "拿不到本机公网 IP"; exit 1; }

v4() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd, - ; }
v6count() { getent ahostsv6 "$1" 2>/dev/null | awk '{print $1}' | sort -u | wc -l | tr -d ' ' ; }
has_self() { echo "$1" | tr ',' '\n' | grep -qx "$IP"; }

ROOT_V4="$(v4 wuyolo.com)";  ROOT_V6="$(v6count wuyolo.com)"
WWW_V4="$(v4 www.wuyolo.com)"; WWW_V6="$(v6count www.wuyolo.com)"

echo "本机公网 IP   : $IP"
echo "wuyolo.com v4 : ${ROOT_V4:-<无>}   v6: $ROOT_V6 条"
echo "www        v4 : ${WWW_V4:-<无>}   v6: $WWW_V6 条"
echo

ROOT_OK=1; has_self "$ROOT_V4" && ROOT_OK=0
WWW_OK=1;  has_self "$WWW_V4"  && WWW_OK=0

if [ "$ROOT_OK" != 0 ] && [ "$FORCE" = 0 ]; then
  cat <<EOF
✗ 根域还没指向本机。去 GoDaddy → DNS 改：
    A  @    → $IP      （原来 185.199.108~111.153 那 4 条要全部删除）
    A  www  → $IP      （原来的 CNAME → *.github.io 要删除）
  改完等 5-30 分钟，用 dig +short wuyolo.com 确认。

  两个选择：
    · 等 DNS 生效后再运行：        sudo bash $0
    · 现在就先把 Caddy 配好（零窗口）：sudo bash $0 --force
EOF
  exit 2
fi

SITES="wuyolo.com"
if [ "$WWW_OK" = 0 ]; then SITES="wuyolo.com, www.wuyolo.com";
else echo "⚠ www 还没指向本机：本次只配 wuyolo.com，www 改好后不带 --force 再跑一次。"; fi
[ "$ROOT_V6" != "0" ] && echo "⚠ 根域仍有 $ROOT_V6 条 IPv6 记录（GitHub Pages 的 AAAA），建议一并删除。"

# 残留检查：指向了本机但还有别的 IP → 会时好时坏
OTHERS="$(echo "$ROOT_V4" | tr ',' '\n' | grep -vx "$IP" | grep -v '^$' | paste -sd, - || true)"
if [ -n "$OTHERS" ]; then
  echo "⚠ 警告：根域还残留其他 IPv4（$OTHERS）——访问会随机打到别处，请把旧记录删干净。"
fi

mkdir -p /srv/home
[ -f /srv/sqa/server/home/index.html ] && cp /srv/sqa/server/home/index.html /srv/home/index.html

cat > /etc/caddy/Caddyfile <<EOF
# 个人主页（静态）
$SITES {
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

if [ "$ROOT_OK" = 0 ]; then
  echo
  echo "✅ 已启用：https://$SITES（证书由 Caddy 自动签发，DNS 生效后 10-30 秒完成）"
else
  echo
  echo "✅ Caddy 已配好（$SITES），等待 DNS 生效后自动签发证书。"
  echo "   现在去改 DNS；改完可运行： curl -I https://wuyolo.com"
fi
