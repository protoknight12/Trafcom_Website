"""
One-off schema migration for the time-based pricing engine refactor (see
calculate_cnc_price() in app.py):

- material_price: drops the old flat cost_per_meter_cut/cost_per_pierce (EUR)
  columns, adds cutting_speed_mm_per_min/pierce_rate_per_min (speed) columns.
  Existing rows get placeholder speed defaults (1000 mm/min, 30 pierces/min) -
  there's no way to back-derive a real machine speed from what the shop used
  to charge per meter/pierce, so admins should retune these via
  /admin/materials after this runs.
- machine: adds machine_type (nullable, admin fills in as needed).
- dxf_file / detail: add a nullable service_id / cutting_service_id FK to the
  new service table.

The `service` and `operation` tables themselves are brand new - db.create_all()
already creates them, no ALTER needed. Run `python app.py` once first (so
db.create_all() creates the `service` table these FKs reference), then run
this migration:

    python -m migration.migrate_services_pricing_refactor

Safe to run more than once (IF NOT EXISTS / IF EXISTS everywhere). A
brand-new database doesn't need this at all - db.create_all() already
creates the current schema directly.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS cutting_speed_mm_per_min FLOAT NOT NULL DEFAULT 1000
    '''))
    db.session.execute(text('''
        ALTER TABLE material_price
        ADD COLUMN IF NOT EXISTS pierce_rate_per_min FLOAT NOT NULL DEFAULT 30
    '''))
    db.session.execute(text('ALTER TABLE material_price DROP COLUMN IF EXISTS cost_per_meter_cut'))
    db.session.execute(text('ALTER TABLE material_price DROP COLUMN IF EXISTS cost_per_pierce'))

    db.session.execute(text('ALTER TABLE machine ADD COLUMN IF NOT EXISTS machine_type VARCHAR(50)'))

    db.session.execute(text('''
        ALTER TABLE dxf_file ADD COLUMN IF NOT EXISTS service_id INTEGER REFERENCES service(id)
    '''))
    db.session.execute(text('''
        ALTER TABLE detail ADD COLUMN IF NOT EXISTS cutting_service_id INTEGER REFERENCES service(id)
    '''))
    db.session.commit()

print("Services/pricing refactor columns added (or already existed).")
