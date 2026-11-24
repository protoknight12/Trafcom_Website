"""
One-off schema migration (owner, 2026-11-24): the laser's technology folder - machine.tech_folder (UNC path of the
machine PC's shared CypCut library folder, e.g. \\\\192.168.18.140\\technology), tech_folder_user and
tech_folder_password_encrypted (Fernet, NETWORK_API_ENCRYPTION_KEY). Safe to run more than once.

    python -m migration.migrate_add_machine_tech_folder
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for col, typ in (('tech_folder', 'VARCHAR(255)'), ('tech_folder_user', 'VARCHAR(100)'),
                     ('tech_folder_password_encrypted', 'TEXT')):
        db.session.execute(text(f'ALTER TABLE machine ADD COLUMN IF NOT EXISTS {col} {typ}'))
    db.session.commit()

print("machine.tech_folder / tech_folder_user / tech_folder_password_encrypted added (or already existed).")
