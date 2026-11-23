"""
One-off schema migration (owner's decision 2026-11-23): a machine's cutting technology keeps everything an FSCUT
nesting file carries - the nozzle (part of the key: Вид + thickness + gas + nozzle), the main cut parameters, three
pierce steps, the whole parameter tree of the channel (params_json) and where it came from (source); a detail gets its
nozzle (detail.cutting_nozzle). All NULL for existing rows. Safe to run more than once.

    python -m migration.migrate_add_cut_tech_fscut_fields
"""
from sqlalchemy import text

from app import app, db

CUT = ['gas_pressure_bar', 'peak_power_w', 'laser_current_pct', 'pwm_freq_hz', 'pwm_ratio_pct', 'focus_mm',
       'height_mm', 'beam_size_mm', 'lift_height_mm', 'pierce_steps']
STEP = ['pressure_bar', 'power_w', 'current_pct', 'freq_hz', 'ratio_pct', 'focus_mm', 'height_mm', 'blow_ms',
        'follow_ms', 'stay_ms']

columns = [('nozzle', 'VARCHAR(30)'), ('params_json', 'TEXT'), ('source', 'VARCHAR(255)')]
columns += [(c, 'DOUBLE PRECISION') for c in CUT]
for n in (1, 2, 3):
    columns.append((f'p{n}_gas', 'VARCHAR(10)'))
    columns += [(f'p{n}_{c}', 'DOUBLE PRECISION') for c in STEP]

with app.app_context():
    for name, sql_type in columns:
        db.session.execute(text(f'ALTER TABLE machine_cut_technology ADD COLUMN IF NOT EXISTS {name} {sql_type}'))
    db.session.execute(text('ALTER TABLE detail ADD COLUMN IF NOT EXISTS cutting_nozzle VARCHAR(30)'))
    db.session.commit()

print(f"machine_cut_technology: {len(columns)} FSCUT columns, detail.cutting_nozzle added (or already existed).")
