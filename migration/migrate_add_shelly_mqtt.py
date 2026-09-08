"""
One-off schema migration: adds `mqtt_topic` to ShellyDevice and makes `host`
nullable (a purely-MQTT device may have no known/reachable IP to poll) - see
start_mqtt_listener() / shelly_device_snapshot(). This only touches the
pre-existing shelly_device table.

    python -m migration.migrate_add_shelly_mqtt

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE shelly_device ADD COLUMN IF NOT EXISTS mqtt_topic VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE shelly_device ALTER COLUMN host DROP NOT NULL
    '''))
    db.session.commit()

print("shelly_device.mqtt_topic added and host made nullable (or already the case).")
