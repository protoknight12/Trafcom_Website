"""
One-off schema migration for the hall machine dossier: hall_machine.manufacturer/serial_number/year/notes and the new
hall_machine_file table (created by db.create_all()). Files are stored under machine_files/<machine_id>/ (private, gitignored).
Safe to run more than once.

    python -m migration.migrate_add_machine_dossier
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()
    for stmt in (
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS manufacturer VARCHAR(150)",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS serial_number VARCHAR(100)",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS year INTEGER",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS notes TEXT",
    ):
        db.session.execute(text(stmt))
    db.session.commit()

print("hall_machine dossier columns + hall_machine_file table ready.")
