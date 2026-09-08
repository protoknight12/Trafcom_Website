"""
One-off schema migration: creates the Vehicle table (new, via
db.create_all() - see Vehicle in app.py). No data is seeded; add vehicles
from /admin/vehicles once this has run.

    python -m migration.seed_vehicles

Safe to run more than once.
"""
from app import app, db

with app.app_context():
    db.create_all()

print("Vehicle table created (or already existed).")
