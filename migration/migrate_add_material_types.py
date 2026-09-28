"""
One-off migration: creates the material_type table (admin-editable material
types - see MaterialType in app.py) and seeds the 5 types that used to be
hardcoded ('sheets'/'rods'/'profiles'/'pipes'/'other', same keys, same
behavior), so existing MaterialPrice.type values keep resolving. No
material_price column changes. Safe to run more than once (create_all only
creates missing tables; seeding only runs on an empty table). Run once:

    python -m migration.migrate_add_material_types
"""
from app import app, db, MaterialType, seed_material_types

with app.app_context():
    MaterialType.__table__.create(db.engine, checkfirst=True)
    seed_material_types()

print("material_type table created and seeded (or already existed).")
