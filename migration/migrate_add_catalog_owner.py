"""
One-off schema migration: adds per-client catalog ownership/visibility.

    python -m migration.migrate_add_catalog_owner

- detail.owner_client_id, product.owner_client_id: which Client a catalog
  entry is private to. NULL (the default for every existing row) means
  public/general-catalog - visible to every client, same as today's
  behavior. Non-NULL restricts it to that one client (+ staff) - see
  _catalog_item_visible() in app.py.

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS owner_client_id INTEGER REFERENCES client(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE product ADD COLUMN IF NOT EXISTS owner_client_id INTEGER REFERENCES client(id)
    '''))
    db.session.commit()

print("detail.owner_client_id and product.owner_client_id added (or already existed).")
