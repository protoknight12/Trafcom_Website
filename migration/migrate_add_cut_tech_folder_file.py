"""
One-off schema migration (owner, 2026-11-24): a technology's link to its .fsm in the machine's technology folder -
machine_cut_technology.folder_file (path relative to machine.tech_folder), folder_file_hash / folder_app_hash (sha256
of the file's XML / of our generated XML at the last sync). Safe to run more than once.

    python -m migration.migrate_add_cut_tech_folder_file
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for col, typ in (('folder_file', 'VARCHAR(500)'), ('folder_file_hash', 'VARCHAR(64)'),
                     ('folder_app_hash', 'VARCHAR(64)')):
        db.session.execute(text(f'ALTER TABLE machine_cut_technology ADD COLUMN IF NOT EXISTS {col} {typ}'))
    db.session.commit()

print("machine_cut_technology.folder_file / folder_file_hash / folder_app_hash added (or already existed).")
