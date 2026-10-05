"""
Runs the one-time data scripts below exactly once per database (the names of the finished ones are kept in the table applied_once), so
deploy/update.sh can call it on every release: schema changes are the migrate_*.py scripts (safe to repeat), data that must not be
applied twice goes here. A script that fails is not recorded and is tried again on the next release.

    python -m migration.run_once            # run what is pending
    python -m migration.run_once --list     # show what is done / pending

To add a step: write migration/<name>.py (it opens its own app context) and append its name to ONCE.
"""
import runpy
import sys

from sqlalchemy import text

from app import app, db

ONCE = (
    'seed_hall_machine_specs',        # cards (page='hall') with the researched specs, hall machines linked to them
    'data_hall_initial_sync',         # machines / rooms / panels for the unified hall map, device placement, empty map positions
    'data_hall_place_rooms',          # every Room without a place on the hall plan gets one
    'data_hall_drop_unplaced_rooms',  # ...and Rooms whose plan shape was deleted are dropped (a room card needs its place)
    'data_import_panels',             # panels + schematics + machine/meter links + communications from the development DB (by name, nothing overwritten)
    'data_hall_follow_rooms',         # plan objects follow the room set on their form; explicit parents naming deleted rooms are cleared
    'data_hall_place_devices',        # devices without a plan object (e.g. the imported panels) get one, so they show in the editor and 3D
    'migrate_add_camera_snapshot_path',  # camera.snapshot_path column (the camera table already existed on some databases)
)

with app.app_context():
    db.session.execute(text("CREATE TABLE IF NOT EXISTS applied_once (name VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    db.session.commit()
    done = {row[0] for row in db.session.execute(text("SELECT name FROM applied_once"))}

if '--list' in sys.argv:
    for name in ONCE:
        print(('done     ' if name in done else 'pending  ') + name)
    sys.exit(0)

for name in ONCE:
    if name in done:
        print(f'-- {name}: already applied')
        continue
    print(f'== {name}')
    runpy.run_module(f'migration.{name}', run_name='__main__')       # raises on failure -> not recorded, update.sh stops (set -e)
    with app.app_context():
        db.session.execute(text("INSERT INTO applied_once (name) VALUES (:n)"), {'n': name})
        db.session.commit()
print('One-time data steps done.')
