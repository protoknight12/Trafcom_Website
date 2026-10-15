#!/usr/bin/env bash
set -e

cd /opt/trafcom
git pull
# git pull may have just replaced this very script, but bash goes on running the old copy it already read -
# start over once with the new one, so a change to update.sh takes effect in the release that brings it
if [ -z "$UPDATE_REEXEC" ]; then UPDATE_REEXEC=1 exec bash /opt/trafcom/deploy/update.sh "$@"; fi

# -c: the versions that are known to work on this server (see the file's header for how to change one)
venv/bin/pip install -r requirements.txt -c deploy/constraints.txt

# Database dump before anything touches the schema - a failed dump aborts the update here,
# with the site still running the old code. SKIP_BACKUP=1 deploy/update.sh to go without.
# ponytail: same disk as the database, so it only covers a bad migration, not a dead disk -
# copy backups/ to another machine (cron + rsync) for that.
if [ -z "$SKIP_BACKUP" ]; then
    mkdir -p backups
    DB_URL=$(venv/bin/python -c "
import os
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
url = os.environ.get('DATABASE_URL') or dotenv_values('.env')['DATABASE_URL']
print(make_url(url).set(drivername='postgresql').render_as_string(hide_password=False))")
    dump="backups/pre_update_$(date +%Y%m%d_%H%M%S).dump"
    echo "== backup -> $dump =="
    pg_dump -Fc "$DB_URL" -f "$dump"
    ls -1t backups/pre_update_*.dump | tail -n +11 | xargs -r rm --   # keep the last 10
fi

venv/bin/python -c "from app import app, db; app.app_context().push(); db.create_all()"

# no writers while migrations run (the poller threads would add rows in the old units mid-rescale)
sudo systemctl stop trafcom
# whatever fails from here on, bring the site back up instead of leaving the shop without it
trap 'echo "!! update failed - starting the site again, check the error above"; sudo systemctl start trafcom' ERR

# every migration/migrate_*.py and data step runs once per database (ledger table applied_once), in one process.
# A schema change = a NEW migrate_*.py file; never edit one that has shipped - it will not run again.
venv/bin/python -m migration.run_once

trap - ERR
sudo systemctl restart trafcom
sudo systemctl status trafcom --no-pager
# /health answers 200 once the app is up and the database responds (503 if it does not); -f turns anything else into a
# failed update instead of looking like success; the retries cover the app's start-up time
curl -fsS -o /dev/null --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/health && echo "== site is up =="
