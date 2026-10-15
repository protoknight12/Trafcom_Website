#!/usr/bin/env bash
# Light release: code/template/static changes only - no new model columns, tables, migrations or dependencies.
# Anything else: deploy/update.sh
set -e

cd /opt/trafcom
git pull
# git pull may have just replaced this very script - start over once with the new copy (see update.sh)
if [ -z "$UPDATE_REEXEC" ]; then UPDATE_REEXEC=1 exec bash /opt/trafcom/deploy/update_quick.sh "$@"; fi

sudo systemctl restart trafcom
sudo systemctl status trafcom --no-pager
# /health answers 200 once the app is up and the database responds; -f turns anything else into a failed update
curl -fsS -o /dev/null --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/health && echo "== site is up =="
