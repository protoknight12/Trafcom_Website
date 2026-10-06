"""Adds camera.detect (object detection on/off per camera); the detection_state / detection_event tables come from db.create_all(). Safe to re-run: python -m migration.migrate_add_camera_detect"""
from sqlalchemy import text
from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE camera ADD COLUMN IF NOT EXISTS detect BOOLEAN NOT NULL DEFAULT FALSE'))
    db.session.commit()
    db.create_all()
    print('ok')
