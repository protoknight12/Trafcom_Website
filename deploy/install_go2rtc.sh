#!/usr/bin/env bash
# One-time (safe to repeat) install of ffmpeg + go2rtc for the camera video. Run on the server: sudo bash /opt/trafcom/deploy/install_go2rtc.sh
# go2rtc listens only on 127.0.0.1:1984 (RTSP only on localhost: its own ffmpeg transcoding reads from it; no WebRTC); streams are registered by the app, the browser reaches it through nginx /camstream/.
set -e
apt-get install -y ffmpeg curl
curl -fL -o /usr/local/bin/go2rtc https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64   # arm64 server: go2rtc_linux_arm64
chmod +x /usr/local/bin/go2rtc
cat > /etc/go2rtc.yaml <<'YAML'
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: "127.0.0.1:8554"
webrtc:
  listen: ""
YAML
cp /opt/trafcom/deploy/go2rtc.service /etc/systemd/system/go2rtc.service
systemctl daemon-reload
systemctl enable --now go2rtc
sleep 1
curl -s http://127.0.0.1:1984/api | head -c 200; echo
echo "go2rtc is up. Now add the /camauth and /camstream/ locations from deploy/nginx_trafcombg.conf to the live nginx site, then: nginx -t && systemctl reload nginx"
