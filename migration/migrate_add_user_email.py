"""
One-off schema migration: adds email + totp_secret columns to User
(email for registration/password-reset, totp_secret for 2FA).

    python -m migration.migrate_add_user_email

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email VARCHAR(150) UNIQUE
    '''))
    db.session.execute(text('''
        ALTER TABLE "user" ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(32)
    '''))
    db.session.commit()

print("user.email and user.totp_secret added (or already existed).")
