"""
One-off schema migration (owner, 2026-11-24): machine_cut_technology.note - the technology's particulars, the <Note>
of a CypCut .fsm (read on import, written on export). Safe to run more than once.

    python -m migration.migrate_add_cut_tech_note
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE machine_cut_technology ADD COLUMN IF NOT EXISTS note TEXT'))
    db.session.commit()

print("machine_cut_technology.note added (or already existed).")
