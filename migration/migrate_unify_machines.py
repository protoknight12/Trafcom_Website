"""
One-off schema migration for the unified machine records: hall_machine.machine_id (1:1 link to Machine, unique) and
hall_machine.on_plan (False = dossier only, not drawn in the 3D hall), plus the new machine_connection table (created by
db.create_all()). Safe to run more than once. Afterwards use the "Create missing machines" button on /machines to create a Machine for
each existing dossier, or link them one by one from the dossier.

    python -m migration.migrate_unify_machines
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()
    for stmt in (
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS machine_id INTEGER REFERENCES machine(id)",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS on_plan BOOLEAN NOT NULL DEFAULT TRUE",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_hall_machine_machine_id ON hall_machine (machine_id)",
    ):
        db.session.execute(text(stmt))
    db.session.commit()

print("hall_machine.machine_id / on_plan and machine_connection ready.")
