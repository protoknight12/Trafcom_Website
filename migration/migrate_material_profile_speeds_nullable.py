"""
One-off schema migration, now a no-op kept only so its name stays in the applied_once ledger: it used to NULL
cutting_speed_mm_per_min/pierce_rate_per_min for every 'profiles' row (profile stock is cut to length, not laser-cut), but
material types are admin-editable now and that UPDATE wiped speeds an admin had entered on every deploy (a new database has no
legacy rows to clear). Both columns are made nullable by migrate_material_rod_speeds_nullable.py.

    python -m migration.migrate_material_profile_speeds_nullable
"""
print("migrate_material_profile_speeds_nullable: nothing to do.")
