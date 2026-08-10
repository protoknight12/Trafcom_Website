"""
One-off schema migration: adds an optional thickness_mm column to detail, a
per-detail override of the material's thickness so editing it from
detail_dxf_dashboard.html's "Материал и размери" tab only affects that one
detail instead of every other detail/product sharing the same material row -
see Detail.thickness_mm in app.py. Safe to run more than once.

    python -m migration.migrate_add_detail_thickness
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS thickness_mm FLOAT
    '''))
    db.session.commit()

print("detail.thickness_mm added (or already present).")
