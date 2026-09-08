"""
One-off schema migration: adds `connection_type` to ShellyDevice (explicit
IP/MQTT/UDP-RPC/CoIoT choice - see CONNECTION_TYPES and ShellyDevice's
docstring). Existing rows are backfilled from their current data: a row with
an mqtt_topic set becomes 'mqtt' (that's what it was actually using),
everything else defaults to 'ip'. This only touches the pre-existing
shelly_device table.

    python -m migration.migrate_add_connection_type

Safe to run more than once, and independent of run order relative to
migrate_add_shelly_mqtt.py - the backfill below reads mqtt_topic, so this
also adds that column itself (identical ADD COLUMN IF NOT EXISTS) rather
than assuming migrate_add_shelly_mqtt.py already ran first.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE shelly_device ADD COLUMN IF NOT EXISTS connection_type VARCHAR(20) NOT NULL DEFAULT 'ip'
    '''))
    db.session.execute(text('''
        ALTER TABLE shelly_device ADD COLUMN IF NOT EXISTS mqtt_topic VARCHAR(150)
    '''))
    db.session.execute(text('''
        UPDATE shelly_device SET connection_type = 'mqtt' WHERE mqtt_topic IS NOT NULL
    '''))
    db.session.commit()

print("shelly_device.connection_type added and backfilled (or already existed).")
