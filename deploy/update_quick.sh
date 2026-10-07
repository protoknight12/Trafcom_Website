#!/usr/bin/env bash
# Light release: code/template/static changes only - no new model columns, tables, migrations or dependencies.
# Anything else: deploy/update.sh
set -e

cd /opt/trafcom
git pull

sudo systemctl restart trafcom
sleep 1
sudo systemctl status trafcom --no-pager
curl -I http://127.0.0.1:8000
