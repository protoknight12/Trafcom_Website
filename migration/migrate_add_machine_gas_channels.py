"""
One-off schema migration (owner, 2026-11-24): machine.gas_channels - which gas channel of a laser each gas goes
through (JSON {gas: channel gas}, e.g. {"air": "nitrogen"} = compressed air on the N2 channel); the FSCUT files built
for the machine carry that channel's GasType. NULL = every gas on its own channel. Safe to run more than once.

    python -m migration.migrate_add_machine_gas_channels
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE machine ADD COLUMN IF NOT EXISTS gas_channels TEXT'))
    db.session.commit()

print("machine.gas_channels added (or already existed).")
