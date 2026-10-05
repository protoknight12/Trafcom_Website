"""Adds camera.conn_type ('nvr'/'onvif'/'rtsp') and camera.port; existing cameras without an NVR become 'rtsp'. Safe to re-run: python -m migration.migrate_add_camera_conn_type"""
from sqlalchemy import text
from app import app, db

with app.app_context():
    cols = db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='camera'")).scalars().all()
    if 'conn_type' not in cols:
        db.session.execute(text("ALTER TABLE camera ADD COLUMN conn_type VARCHAR(6) NOT NULL DEFAULT 'nvr'"))
        db.session.execute(text("UPDATE camera SET conn_type = 'rtsp' WHERE nvr_id IS NULL"))
    db.session.execute(text("ALTER TABLE camera ADD COLUMN IF NOT EXISTS port INTEGER"))
    db.session.commit()
    print('ok')
