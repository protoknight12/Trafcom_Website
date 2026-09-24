"""
One-off schema migration: adds NetworkDevice.api_enable_secret_encrypted for
live Cisco IOS management over SSH (netmiko) - see app.py's
"LAN / МРЕЖОВА ИНФРАСТРУКТУРА" section, live-management part.

    python -m migration.migrate_add_network_cisco_ssh

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE network_device ADD COLUMN IF NOT EXISTS api_enable_secret_encrypted TEXT
    '''))
    db.session.commit()

print("network_device.api_enable_secret_encrypted added (or already existed).")
