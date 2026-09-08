"""
One-off schema migration + data seed: adds modbus_device.source_device_id
(self-referential FK) and seeds one 'solis_grid_meter' row - a *virtual*
measuring device with no Modbus connection of its own, representing the
smart meter physically mounted in "Главно разпределително табло" that's
actually read out through Inverter 1's own Modbus link (see
ModbusDevice.source_device_id's docstring in app.py and
_solis_grid_meter_view_snapshot()).

    python -m migration.migrate_add_solis_grid_meter

Safe to run more than once - skips seeding if a row with that name already
exists, and does nothing if Inverter 1 or the target panel can't be found.
"""
from sqlalchemy import text

from app import app, db, ModbusDevice, ElectricalPanel

NAME = 'Главно разпределително табло - смарт метър'
PANEL_NAME = 'Главно разпределително табло'

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE modbus_device ADD COLUMN IF NOT EXISTS source_device_id INTEGER
        REFERENCES modbus_device(id)
    '''))
    db.session.commit()

    if ModbusDevice.query.filter_by(name=NAME).first():
        print(f'"{NAME}": already seeded, skipping.')
    else:
        inverters = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all()
        if not inverters:
            print('No solis_s6 inverter found - not seeding.')
        else:
            inverter_1 = inverters[0]
            panel = ElectricalPanel.query.filter_by(name=PANEL_NAME).first()
            if not panel:
                print(f'Panel "{PANEL_NAME}" not found - not seeding.')
            else:
                db.session.add(ModbusDevice(
                    name=NAME, host=inverter_1.host, port=inverter_1.port, unit_id=inverter_1.unit_id,
                    device_type='solis_grid_meter', panel_id=panel.id, source_device_id=inverter_1.id,
                    notes='Смарт метър за мрежово захранване/износ, физически монтиран в '
                          f'"{PANEL_NAME}" - отчита се по Modbus през "{inverter_1.name}" '
                          '(неговия собствен блок "meter"), без отделна Modbus връзка.',
                ))
                db.session.commit()
                print(f'"{NAME}": seeded (source={inverter_1.name}, panel={panel.name}).')

print('Done.')
