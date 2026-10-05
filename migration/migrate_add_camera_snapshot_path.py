"""Adds camera.snapshot_path (own snapshot URL path for non-Hikvision cameras). Safe to re-run: python -m migration.migrate_add_camera_snapshot_path"""
from sqlalchemy import text
from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE camera ADD COLUMN IF NOT EXISTS snapshot_path VARCHAR(200)'))
    db.session.commit()
    print('ok')
