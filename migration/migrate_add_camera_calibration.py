"""Adds camera.roll and camera.calib_json (camera pose from known objects). Safe to re-run: python -m migration.migrate_add_camera_calibration"""
from sqlalchemy import text
from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE camera ADD COLUMN IF NOT EXISTS roll DOUBLE PRECISION NOT NULL DEFAULT 0'))
    db.session.execute(text('ALTER TABLE camera ADD COLUMN IF NOT EXISTS calib_json TEXT'))
    db.session.commit()
    print('ok')
