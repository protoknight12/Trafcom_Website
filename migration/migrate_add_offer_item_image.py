"""
One-off schema migration: adds offer_item.image_filename - the optional
per-line photo shown in the offer editor and embedded into the exported
.xlsx (see upload_offer_item_image()/build_offer_workbook() in app.py).
Safe to run more than once.

    python -m migration.migrate_add_offer_item_image
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE offer_item
        ADD COLUMN IF NOT EXISTS image_filename VARCHAR(255)
    '''))
    db.session.commit()

print("offer_item.image_filename added (or already existed).")
