"""
One-off schema migration: adds the LAN network management module
(NetworkDevice, NetworkLink, NetworkHost) - see app.py's
"LAN / МРЕЖОВА ИНФРАСТРУКТУРА" section.

    python -m migration.migrate_add_network_module

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        CREATE TABLE IF NOT EXISTS network_device (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            vendor VARCHAR(20) NOT NULL DEFAULT 'other',
            device_role VARCHAR(20) NOT NULL DEFAULT 'other',
            model VARCHAR(100),
            management_ip VARCHAR(45) UNIQUE,
            mac_address VARCHAR(17),
            management_vlan INTEGER,
            notes TEXT,
            pos_x FLOAT,
            pos_y FLOAT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    '''))
    db.session.execute(text('''
        CREATE TABLE IF NOT EXISTS network_link (
            id SERIAL PRIMARY KEY,
            device_a_id INTEGER NOT NULL REFERENCES network_device(id),
            device_b_id INTEGER NOT NULL REFERENCES network_device(id),
            link_type VARCHAR(20) NOT NULL DEFAULT 'copper',
            vlan INTEGER,
            port_a VARCHAR(50),
            port_b VARCHAR(50),
            notes TEXT
        )
    '''))
    db.session.execute(text('''
        CREATE TABLE IF NOT EXISTS network_host (
            id SERIAL PRIMARY KEY,
            mac_address VARCHAR(17) NOT NULL UNIQUE,
            hostname VARCHAR(150),
            ip_address VARCHAR(45),
            ip_mode VARCHAR(10) NOT NULL DEFAULT 'dynamic',
            device_type VARCHAR(100),
            switch_device_id INTEGER REFERENCES network_device(id),
            switch_port VARCHAR(50),
            notes TEXT,
            last_seen TIMESTAMP,
            source VARCHAR(20) NOT NULL DEFAULT 'manual',
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    '''))
    db.session.commit()

print("network_device, network_link, and network_host tables added (or already existed).")
