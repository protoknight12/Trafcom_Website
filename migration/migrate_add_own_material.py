"""
One-off schema migration: the "Материал на клиента" option.

    python -m migration.migrate_add_own_material

- client.own_material_surcharge_percent: extra % on services when a line uses the client's own material
- order_item.own_material / dxf_file.own_material: that line/upload was priced that way

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE client ADD COLUMN IF NOT EXISTS own_material_surcharge_percent FLOAT'))
    db.session.execute(text('ALTER TABLE order_item ADD COLUMN IF NOT EXISTS own_material BOOLEAN NOT NULL DEFAULT FALSE'))
    db.session.execute(text('ALTER TABLE dxf_file ADD COLUMN IF NOT EXISTS own_material BOOLEAN NOT NULL DEFAULT FALSE'))
    db.session.commit()

print('own_material columns added (or already existed).')
