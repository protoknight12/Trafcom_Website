"""
One-off schema migration: creates the Convector table (new, via
db.create_all() - see Convector in app.py). No data is seeded; add
convectors from /admin/convectors once this has run.

    python -m migration.seed_convectors

Safe to run more than once.
"""
from app import app, db

with app.app_context():
    db.create_all()

print("Convector table created (or already existed).")
