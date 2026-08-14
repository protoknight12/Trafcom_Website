"""
One-off schema migration: clears cutting_speed_mm_per_min/pierce_rate_per_min
for existing 'profiles' rows - profile stock is cut to length, not laser-cut/
pierced, same reasoning already applied to 'rods' in
migrate_material_rod_speeds_nullable.py. Both columns are already nullable
(from that earlier migration), so this only backfills existing data. Safe to
run more than once.

    python -m migration.migrate_material_profile_speeds_nullable
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        UPDATE material_price SET cutting_speed_mm_per_min = NULL, pierce_rate_per_min = NULL WHERE type = 'profiles'
    '''))
    db.session.commit()

print("material_price.cutting_speed_mm_per_min/pierce_rate_per_min cleared for existing 'profiles' rows (or already done).")
