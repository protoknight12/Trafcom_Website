"""
One-off schema migration: adds order_item.material_key - the material a standalone-detail order
line was ordered in when it is not the detail's default (one of the detail's allowed materials,
DetailMaterial; see app.py's _line_material()). Existing lines keep NULL = the detail's default,
exactly as before. The detail_material table itself comes from db.create_all(). Safe to run more
than once.

    python -m migration.migrate_add_order_item_material
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE order_item
        ADD COLUMN IF NOT EXISTS material_key VARCHAR(50) REFERENCES material_price(key)
    '''))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_order_item_material_key ON order_item (material_key)'))
    db.session.commit()

print("order_item.material_key added (or already existed).")
