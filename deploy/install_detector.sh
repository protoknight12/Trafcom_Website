#!/usr/bin/env bash
# One-time (safe to repeat): downloads the object-detection model (YOLOX-s, Apache-2.0, ~35 MB) to /opt/trafcom/models.
# Run as the app user or with sudo, after "git pull" + deploy/update.sh (which installs onnxruntime/numpy/pillow from requirements.txt).
set -e
mkdir -p /opt/trafcom/models /opt/trafcom/detection_files
curl -fL -o /opt/trafcom/models/yolox_s.onnx.new https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx
mv -f /opt/trafcom/models/yolox_s.onnx.new /opt/trafcom/models/yolox_s.onnx
chown -R www-data:www-data /opt/trafcom/models /opt/trafcom/detection_files
echo "Model ready. Restart the app: sudo systemctl restart trafcom"
