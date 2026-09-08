"""
One-off schema migration: adds optional English/German translation columns
to the customer-facing catalog tables (Product, Service, MaterialPrice,
Detail) - see localized() in app.py. All columns are nullable; an empty
translation falls back to the Bulgarian base field, so existing rows keep
working untouched.

    python -m migration.migrate_add_i18n_fields

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE product ADD COLUMN IF NOT EXISTS name_en VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE product ADD COLUMN IF NOT EXISTS name_de VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE product ADD COLUMN IF NOT EXISTS description_en TEXT
    '''))
    db.session.execute(text('''
        ALTER TABLE product ADD COLUMN IF NOT EXISTS description_de TEXT
    '''))
    db.session.execute(text('''
        ALTER TABLE service ADD COLUMN IF NOT EXISTS name_en VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE service ADD COLUMN IF NOT EXISTS name_de VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE service ADD COLUMN IF NOT EXISTS description_en TEXT
    '''))
    db.session.execute(text('''
        ALTER TABLE service ADD COLUMN IF NOT EXISTS description_de TEXT
    '''))
    db.session.execute(text('''
        ALTER TABLE material_price ADD COLUMN IF NOT EXISTS display_name_en VARCHAR(100)
    '''))
    db.session.execute(text('''
        ALTER TABLE material_price ADD COLUMN IF NOT EXISTS display_name_de VARCHAR(100)
    '''))
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS name_en VARCHAR(150)
    '''))
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS name_de VARCHAR(150)
    '''))
    db.session.commit()

print("i18n translation columns added to product/service/material_price/detail (or already existed).")
