"""
One-off schema migration: adds grid_panel_id/main_panel_id/backup_panel_id/
generator_panel_id to ModbusDevice - which ElectricalPanel each of a Solis
inverter's 4 AC ports (Мрежа/Основен/Бекъп/Генератор) actually feeds/
connects to, independent of `panel_id` (where the inverter's own meter is
physically mounted) - see ModbusDevice in app.py.

    python -m migration.migrate_add_solis_port_panels

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for column in ('grid_panel_id', 'main_panel_id', 'backup_panel_id', 'generator_panel_id'):
        db.session.execute(text(f'''
            ALTER TABLE modbus_device ADD COLUMN IF NOT EXISTS {column} INTEGER REFERENCES electrical_panel(id)
        '''))
    db.session.commit()

print("modbus_device.grid_panel_id/main_panel_id/backup_panel_id/generator_panel_id added (or already existed).")
