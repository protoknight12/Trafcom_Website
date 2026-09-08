"""
One-off schema migration: creates the Cabinet, BatteryStack and Battery
tables (new, via db.create_all() - see those classes in app.py). No data is
seeded; add cabinets/stacks/batteries from /admin/battery-cabinets once this
has run.

    python -m migration.seed_battery_inventory

Safe to run more than once.
"""
from app import app, db

with app.app_context():
    db.create_all()

print("Cabinet/BatteryStack/Battery tables created (or already existed).")
