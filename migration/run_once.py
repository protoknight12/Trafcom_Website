"""
Runs every migration exactly once per database (the names of the finished ones are kept in the table applied_once), so
deploy/update.sh just calls this on every release. In ONE Python process (the app is imported once), in this order:

  1. every migration/migrate_*.py not yet recorded, sorted by name  (schema changes)
  2. the ONCE data steps below, in their listed order               (data that must not be applied twice)

RULE: a schema change is a NEW migrate_*.py file. Never edit one that has shipped - it is recorded and will not run again.
A script that fails is not recorded: the run stops with a non-zero exit (update.sh stops, set -e) and it is tried again next release.
A script may end with sys.exit(0). A brand-new database needs none of them (db.create_all() builds the current schema), they all
just run once and get recorded.

    python -m migration.run_once            # run what is pending
    python -m migration.run_once --list     # show what is done / pending

To add a step: write migration/<name>.py (it opens its own app context). Schema -> name it migrate_*.py, nothing else to do.
Data step -> append its name to ONCE.
"""
import glob
import os
import runpy
import sys

from sqlalchemy import text

from app import app, db

PACKAGE = 'migration'
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

ONCE = (
    'seed_hall_machine_specs',        # cards (page='hall') with the researched specs, hall machines linked to them
    'data_hall_initial_sync',         # machines / rooms / panels for the unified hall map, device placement, empty map positions
    'data_hall_place_rooms',          # every Room without a place on the hall plan gets one
    'data_hall_drop_unplaced_rooms',  # ...and Rooms whose plan shape was deleted are dropped (a room card needs its place)
    'data_import_panels',             # panels + schematics + machine/meter links + communications from the development DB (by name, nothing overwritten)
    'data_hall_follow_rooms',         # plan objects follow the room set on their form; explicit parents naming deleted rooms are cleared
    'data_hall_place_devices',        # devices without a plan object (e.g. the imported panels) get one, so they show in the editor and 3D
    'migrate_add_camera_snapshot_path',  # camera.snapshot_path column (the camera table already existed on some databases)
    'data_shelly_gen1_energy_x60',    # Gen1 meters' logged energy was 60x too low (Wh read as Watt-minutes): rescale the old ShellyReadingLog rows
    'data_close_registration',        # owner's decision 2026-10-15: new accounts only through an admin (checkbox on /admin/users reopens it)
)


def schema_scripts():
    """migrate_* module names found on disk, sorted; the ones listed in ONCE run there instead (once, in that order)."""
    names = sorted(os.path.basename(f)[:-3] for f in glob.glob(os.path.join(DIRECTORY, 'migrate_*.py')))
    return [n for n in names if n not in ONCE]


def main(argv):
    with app.app_context():
        db.session.execute(text("CREATE TABLE IF NOT EXISTS applied_once (name VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
        db.session.commit()
        done = {row[0] for row in db.session.execute(text("SELECT name FROM applied_once"))}

    steps = schema_scripts() + list(ONCE)
    if '--list' in argv:
        for name in steps:
            print(('done     ' if name in done else 'pending  ') + name)
        return 0

    for name in steps:
        if name in done:
            print(f'-- {name}: already applied')
            continue
        print(f'== {name}')
        try:
            runpy.run_module(f'{PACKAGE}.{name}', run_name='__main__')   # raises on failure -> not recorded, update.sh stops (set -e)
        except SystemExit as e:
            if e.code not in (None, 0):
                raise
        with app.app_context():
            db.session.execute(text("INSERT INTO applied_once (name) VALUES (:n)"), {'n': name})
            db.session.commit()
    print('Migrations done.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
