"""
One-off schema migration: adds live RouterOS API management to the LAN
network module (NetworkDevice.is_dhcp_server/api_username/api_password_encrypted)
- see app.py's "LAN / МРЕЖОВА ИНФРАСТРУКТУРА" section, live-management part.

    python -m migration.migrate_add_network_dhcp_api

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE network_device ADD COLUMN IF NOT EXISTS is_dhcp_server BOOLEAN NOT NULL DEFAULT FALSE
    '''))
    db.session.execute(text('''
        ALTER TABLE network_device ADD COLUMN IF NOT EXISTS api_username VARCHAR(100)
    '''))
    db.session.execute(text('''
        ALTER TABLE network_device ADD COLUMN IF NOT EXISTS api_password_encrypted TEXT
    '''))
    db.session.commit()

print("network_device.is_dhcp_server/api_username/api_password_encrypted added (or already existed).")
