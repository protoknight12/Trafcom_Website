"""
One-off schema migration: Convector.mqtt_topic is no longer unique - one Shelly
with two outputs feeds two convectors on the same MQTT topic (different
relay_channel).

    python -m migration.migrate_convector_topic_not_unique

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text("ALTER TABLE convector DROP CONSTRAINT IF EXISTS convector_mqtt_topic_key"))
    db.session.commit()

print("convector.mqtt_topic unique constraint dropped (or already gone).")
