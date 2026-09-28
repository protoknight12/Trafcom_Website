"""
One-off schema migration: per-client Продукти discount/markup.

    python -m migration.migrate_add_client_product_pricing

- client.product_adjustment_type/percent: applied to a Product's whole sell
  price (calculate_product_pricing). Products no longer get the client's
  Детайли terms, so on the first run every client's current Детайли terms
  are copied into the new Продукти ones - their product prices stay about
  where they were (the % now applies to the whole price, not just the
  details' share of it).

Safe to run more than once: the copy only happens when the columns are
first added, so a later run never overwrites terms an admin has since set.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    existed = db.session.execute(text('''
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'client' AND column_name = 'product_adjustment_type'
    ''')).first() is not None
    db.session.execute(text('''
        ALTER TABLE client ADD COLUMN IF NOT EXISTS product_adjustment_type VARCHAR(10)
    '''))
    db.session.execute(text('''
        ALTER TABLE client ADD COLUMN IF NOT EXISTS product_adjustment_percent FLOAT
    '''))
    copied = 0
    if not existed:
        copied = db.session.execute(text('''
            UPDATE client SET product_adjustment_type = detail_adjustment_type,
                              product_adjustment_percent = detail_adjustment_percent
            WHERE detail_adjustment_type IS NOT NULL
        ''')).rowcount
    db.session.commit()

print(f"client.product_adjustment_* added (or already existed); Детайли terms copied to {copied} client(s).")
