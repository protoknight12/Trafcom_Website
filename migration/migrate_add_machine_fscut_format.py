"""
One-off schema migration (owner's decision 2026-11-23): which FSCUT file format a laser machine's CypCutE reads
(machine.fscut_format: 'lxd' old / 'lxds' new, NULL = not set) - decides which file "Изтегли за машината" builds.
Safe to run more than once.

    python -m migration.migrate_add_machine_fscut_format
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE machine ADD COLUMN IF NOT EXISTS fscut_format VARCHAR(5)'))
    db.session.commit()

print("machine.fscut_format added (or already existed).")
