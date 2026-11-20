"""
One-off schema migration (owner's decision 2026-11-20): machine.is_laser - a laser-cutting machine (offered as a
Detail's cutting machine, has "Технологии на рязане" in its dossier). Machines whose type or name mentions laser /
лазер are marked once, when the column is first added; after that the checkbox is the only source. Safe to run more
than once.

    python -m migration.migrate_add_machine_is_laser
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    existed = db.session.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'machine' AND column_name = 'is_laser'")).first()
    db.session.execute(text('ALTER TABLE machine ADD COLUMN IF NOT EXISTS is_laser BOOLEAN NOT NULL DEFAULT FALSE'))
    n = 0
    if not existed:
        n = db.session.execute(text(
            "UPDATE machine SET is_laser = TRUE WHERE LOWER(COALESCE(machine_type, '') || ' ' || name) LIKE '%laser%' "
            "OR LOWER(COALESCE(machine_type, '') || ' ' || name) LIKE '%лазер%'")).rowcount
    db.session.commit()

print(f"machine.is_laser added (or already existed); {n} machines marked as laser.")
