#!/usr/bin/env bash
# One-time (safe to repeat): adds the /camauth + /camstream/ locations to the live nginx site. Run: sudo bash /opt/trafcom/deploy/install_nginx_cam.sh
# Works with a certbot/HTTPS site too: the locations live in a snippet that is include'd inside every server block that has "location / {".
set -e
SNIP=/etc/nginx/snippets/trafcom_cam.conf
mkdir -p /etc/nginx/snippets
cat > "$SNIP" <<'CONF'
location = /camauth {
    internal;
    proxy_pass http://127.0.0.1:8000/admin/cameras/auth;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Host $host;
}
location ~ ^/camstream/api/(stream\.mjpeg|frame\.jpeg)$ {
    auth_request /camauth;
    if ($arg_src !~ "^cam[0-9]+$") { return 403; }
    rewrite ^/camstream/(.*)$ /$1 break;
    proxy_pass http://127.0.0.1:1984;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
CONF

# the live site = the enabled file that proxies to gunicorn on :8000
SITE=$(grep -lE "proxy_pass +http://127\.0\.0\.1:8000" /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null | head -1)
[ -n "$SITE" ] || { echo "Не намирам nginx сайта (proxy_pass към 127.0.0.1:8000)."; exit 1; }
SITE=$(readlink -f "$SITE")
echo "Сайт: $SITE"

if grep -q "snippets/trafcom_cam.conf" "$SITE"; then
  echo "include вече е добавен."
else
  cp "$SITE" "$SITE.bak.$(date +%s)"
  sed -i -E 's|^([[:space:]]*)location / \{|\1include /etc/nginx/snippets/trafcom_cam.conf;\n\1location / {|' "$SITE"
  grep -q "snippets/trafcom_cam.conf" "$SITE" || { echo "Не намерих 'location / {' в $SITE - добавете ръчно: include $SNIP;"; exit 1; }
fi

nginx -t
systemctl reload nginx
echo "nginx е готов. Презаредете страницата с камерата."
