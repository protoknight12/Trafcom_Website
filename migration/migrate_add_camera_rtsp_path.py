"""Adds camera.rtsp_path (RTSP path of an own-IP camera for the go2rtc video). Safe to re-run: python -m migration.migrate_add_camera_rtsp_path"""
from sqlalchemy import text
from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE camera ADD COLUMN IF NOT EXISTS rtsp_path VARCHAR(200)'))
    db.session.commit()
    print('ok')
