"""
One-off schema migration (owner's decision 2026-11-20): the laser assist gas (CUT_GASES: oxygen / nitrogen / air) on
a machine's cutting technology (machine_cut_technology.gas, part of its key: Вид + thickness + gas) and on a detail
(detail.cutting_gas). NULL = not set. Safe to run more than once.

    python -m migration.migrate_add_cut_gas
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE machine_cut_technology ADD COLUMN IF NOT EXISTS gas VARCHAR(10)'))
    db.session.execute(text('ALTER TABLE detail ADD COLUMN IF NOT EXISTS cutting_gas VARCHAR(10)'))
    db.session.commit()

print("machine_cut_technology.gas and detail.cutting_gas added (or already existed).")
