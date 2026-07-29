#!/usr/bin/env bash
set -e

cd /opt/trafcom
git pull
venv/bin/pip install -r requirements.txt
venv/bin/python -c "from app import app, db; app.app_context().push(); db.create_all()"

for f in migration/migrate_*.py; do
    mod="migration.$(basename "$f" .py)"
    echo "== $mod =="
    venv/bin/python -m "$mod"
done

sudo systemctl restart trafcom
sleep 1
sudo systemctl status trafcom --no-pager
curl -I http://127.0.0.1:8000
