"""
One-off schema migration (owner's decision 2026-11-20): cutting speed and pierce time move from the material to the
detail, per machine. Adds detail.cutting_machine_id / cutting_speed_mm_per_min / pierce_time_sec and copies every
existing detail's current values from its material (pierce time = 60 / pierce_rate_per_min seconds) - only where
the detail has none yet, so it is safe to run more than once. The machine_cut_technology table comes from
db.create_all() (also called here).

    python -m migration.migrate_add_detail_cut_technology
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.create_all()
    for ddl in ('ALTER TABLE detail ADD COLUMN IF NOT EXISTS cutting_machine_id INTEGER REFERENCES machine(id)',
                'ALTER TABLE detail ADD COLUMN IF NOT EXISTS cutting_speed_mm_per_min DOUBLE PRECISION',
                'ALTER TABLE detail ADD COLUMN IF NOT EXISTS pierce_time_sec DOUBLE PRECISION',
                'CREATE INDEX IF NOT EXISTS ix_detail_cutting_machine_id ON detail (cutting_machine_id)'):
        db.session.execute(text(ddl))
    n = db.session.execute(text(
        'UPDATE detail SET cutting_speed_mm_per_min = m.cutting_speed_mm_per_min, '
        'pierce_time_sec = CASE WHEN m.pierce_rate_per_min > 0 THEN ROUND(CAST(60.0 / m.pierce_rate_per_min AS NUMERIC), 3) END '
        'FROM material_price m WHERE m.key = detail.material_key '
        'AND detail.cutting_speed_mm_per_min IS NULL AND detail.pierce_time_sec IS NULL')).rowcount
    db.session.commit()

print(f"detail cutting technology columns added; {n} details got their material's speeds.")
