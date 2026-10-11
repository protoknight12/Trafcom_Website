"""
One-off schema migration: press_brake_config.x_max_mm / r_max_mm (back gauge travel on X and R) and finger_count. Only needed if the table already existed.

    python -m migration.migrate_add_press_kinematics

Safe to run more than once. Then `python -m migration.seed_press_brake_configs` fills the AD-ES 2040 values (X 650, R 250, from its manual).
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for col, typ in (("x_max_mm", "DOUBLE PRECISION"), ("r_max_mm", "DOUBLE PRECISION"), ("finger_count", "INTEGER"), ("kinematics_json", "TEXT")):
        db.session.execute(text(f"ALTER TABLE press_brake_config ADD COLUMN IF NOT EXISTS {col} {typ}"))
    db.session.commit()

print("press_brake_config.x_max_mm / r_max_mm / finger_count added (or already existed).")
