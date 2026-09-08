"""
One-off schema migration: adds MQTT support to Convector - connection_type
(default 'ip', matching every existing row's original HTTP-only behavior),
mqtt_topic (new, nullable+unique), and relaxes host to nullable (an
MQTT-only convector has no HTTP address at all - see app.py's Convector
model and _shelly_convector_status()/_shelly_convector_set()).

    python -m migration.migrate_add_convector_mqtt

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE convector ADD COLUMN IF NOT EXISTS connection_type VARCHAR(10) NOT NULL DEFAULT 'ip'
    '''))
    db.session.execute(text('''
        ALTER TABLE convector ADD COLUMN IF NOT EXISTS mqtt_topic VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE convector ALTER COLUMN host DROP NOT NULL
    '''))
    # mqtt_topic needs to be unique (mirrors ShellyDevice/TemperatureSensor)
    # but only once, and IF NOT EXISTS isn't valid syntax for ADD CONSTRAINT -
    # check pg_constraint first so re-running this script doesn't error.
    exists = db.session.execute(text('''
        SELECT 1 FROM pg_constraint WHERE conname = 'convector_mqtt_topic_key'
    ''')).first()
    if not exists:
        db.session.execute(text('''
            ALTER TABLE convector ADD CONSTRAINT convector_mqtt_topic_key UNIQUE (mqtt_topic)
        '''))
    db.session.commit()

print("convector.connection_type/mqtt_topic added, host relaxed to nullable (or already done).")
