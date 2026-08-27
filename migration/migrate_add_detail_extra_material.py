"""
One-off schema migration: adds extra_width_mm/extra_height_mm to detail - an
optional stock margin (mm) added on top of width/height before pricing (e.g.
a couple mm of scrap allowance left on each edge), editable from the
"Материал и размери" tab on detail_dxf_dashboard.html. See Detail in app.py.
Safe to run more than once.

    python -m migration.migrate_add_detail_extra_material
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS extra_width_mm FLOAT
    '''))
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS extra_height_mm FLOAT
    '''))
    db.session.commit()

print("detail.extra_width_mm / extra_height_mm added (or already present).")
