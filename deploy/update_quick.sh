#!/usr/bin/env bash
# Light release: code/template/static changes only - no new model columns, tables, migrations or dependencies.
# Anything else: deploy/update.sh
set -e

cd /opt/trafcom
git pull

sudo systemctl restart trafcom
sudo systemctl status trafcom --no-pager
# -f: a 500 from the app fails the update instead of looking like success; retries cover the app's start-up time
curl -fsS -o /dev/null --retry 15 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000 && echo "== site is up =="
