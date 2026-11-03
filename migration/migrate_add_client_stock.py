"""
One-off schema migration: client stock (материали на съхранение).

    python -m migration.migrate_add_client_stock

- material_price.owner_client_id: NULL = our lot, else the client who owns it (+ index)
- delivery_note.client_id: a receipt FROM a client leaving material for storage
- client_delivery_note.kind: 'return' (issue/return to client) or 'consume' (write-off for production)
- client_delivery_note_item.remnant_*: leftover returned to stock by a 'consume' line

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE material_price ADD COLUMN IF NOT EXISTS owner_client_id INTEGER REFERENCES client(id)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_material_price_owner_client_id ON material_price(owner_client_id)'))
    db.session.execute(text('ALTER TABLE delivery_note ADD COLUMN IF NOT EXISTS client_id INTEGER REFERENCES client(id)'))
    db.session.execute(text("ALTER TABLE client_delivery_note ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'return'"))
    db.session.execute(text('ALTER TABLE client_delivery_note_item ADD COLUMN IF NOT EXISTS remnant_qty FLOAT'))
    db.session.execute(text('ALTER TABLE client_delivery_note_item ADD COLUMN IF NOT EXISTS remnant_width FLOAT'))
    db.session.execute(text('ALTER TABLE client_delivery_note_item ADD COLUMN IF NOT EXISTS remnant_height FLOAT'))
    db.session.execute(text('ALTER TABLE client_delivery_note_item ADD COLUMN IF NOT EXISTS remnant_material_id INTEGER REFERENCES material_price(id)'))
    db.session.commit()

print('client stock columns added (or already existed).')
