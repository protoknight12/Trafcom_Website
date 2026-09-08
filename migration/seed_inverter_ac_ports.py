"""
One-off data seed: adds the 4 real AC ports (Мрежа/Основен/Бекъп/Генератор -
see ModbusDevice.grid_panel_id/main_panel_id/backup_panel_id/
generator_panel_id in app.py) as selectable PanelComponent rows on each
Solis inverter's own ElectricalPanel row, so they show up as connectable
targets in the panel-schematic editor's cross-panel "Свържи с елемент от
друго табло" picker (admin_panel_schematic.html) - explicit ask: "когато
избера инвертор за връзка да мога да избирам на кой порт на инвертора".
Inverters otherwise have zero PanelComponent rows (they aren't a DIN-rail
schematic, they're a Solis S6), so the picker would show nothing to choose.

    python -m migration.seed_inverter_ac_ports

Safe to run more than once - skips a panel already seeded (matched by name).
"""
from app import app, db, ElectricalPanel, PanelComponent

PORTS = [
    ('Мрежа (Grid)', 20.0),
    ('Основен (Main)', 40.0),
    ('Бекъп (Backup)', 60.0),
    ('Генератор', 80.0),
]

with app.app_context():
    inverters = ElectricalPanel.query.filter(ElectricalPanel.name.ilike('Инвертор%')).all()
    for panel in inverters:
        existing_names = {c.name for c in PanelComponent.query.filter_by(panel_id=panel.id).all()}
        added = 0
        for name, pos_x in PORTS:
            if name in existing_names:
                continue
            db.session.add(PanelComponent(
                panel_id=panel.id, component_type='terminal', name=name,
                poles=3, pos_x=pos_x, pos_y=50.0,
            ))
            added += 1
        print(f'{panel.name} (id={panel.id}): added {added} port(s), {len(existing_names)} already present.')
    db.session.commit()

print('Done.')
