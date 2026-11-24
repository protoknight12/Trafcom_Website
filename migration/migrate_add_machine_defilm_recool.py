"""
One-off schema migration (owner, 2026-11-25): a laser's CypCut Defilm / Recool technologies, one per machine -
machine.defilm_json and machine.recool_json (the .fsm's parameter tree as JSON). Safe to run more than once.

    python -m migration.migrate_add_machine_defilm_recool
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for col in ('defilm_json', 'recool_json'):
        db.session.execute(text(f'ALTER TABLE machine ADD COLUMN IF NOT EXISTS {col} TEXT'))
    db.session.commit()

print("machine.defilm_json / recool_json added (or already existed).")
