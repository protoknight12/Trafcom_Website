"""
Exports the electrical panels (with their schematics), the machine/meter -> panel links and the machines' communications from the
database it runs against into migration/data/panels_export.json - so they can be loaded into another database (production) by
migration/data_import_panels.py. Everything is written by NAME (room / panel / machine / meter host / network device), never by id.

    python -m migration.export_panels
"""
import json
import os
from datetime import datetime

from app import (app, ElectricalPanel, PanelComponent, PanelWire, Machine, ShellyDevice, ModbusDevice, MachineConnection, HallMachine,
                 NetworkHost, NetworkDevice, Room, HallShape)

OUT = os.path.join(os.path.dirname(__file__), 'data', 'panels_export.json')
SKIP = {'id', 'created_at'}
MODBUS_PANEL_COLS = ('panel_id', 'grid_panel_id', 'main_panel_id', 'backup_panel_id', 'generator_panel_id')


def plain(v):
    return v.isoformat() if isinstance(v, datetime) else v


def row(obj, drop=()):
    return {c.name: plain(getattr(obj, c.name)) for c in obj.__table__.columns if c.name not in SKIP and c.name not in drop}


with app.app_context():
    pname = lambda pid: db_get(ElectricalPanel, pid)
    from app import db
    db_get = lambda M, i: db.session.get(M, i) if i else None
    nm = lambda M, i: (db_get(M, i).name if db_get(M, i) else None)
    host_of = lambda M, i: (db_get(M, i).host if db_get(M, i) else None)

    panels = []
    for p in ElectricalPanel.query.order_by(ElectricalPanel.id):
        d = row(p, ('room_id', 'parent_panel_id'))
        d['room'] = nm(Room, p.room_id)
        d['parent'] = nm(ElectricalPanel, p.parent_panel_id)
        d['components'] = []
        for c in p.components:
            cd = row(c, ('panel_id', 'feeds_machine_id', 'feeds_panel_id', 'feeds_modbus_device_id'))
            cd['ref'] = c.id
            cd['feeds_machine'], cd['feeds_panel'] = nm(Machine, c.feeds_machine_id), nm(ElectricalPanel, c.feeds_panel_id)
            cd['feeds_modbus'] = host_of(ModbusDevice, c.feeds_modbus_device_id)
            d['components'].append(cd)
        d['wires'] = [dict(row(w, ('panel_id', 'from_component_id', 'to_component_id')), frm=w.from_component_id, to=w.to_component_id) for w in p.wires]
        panels.append(d)

    rooms = {}                                                           # every room a panel stands in: its building + its place on the hall plan
    for r in Room.query:
        if r.name not in {p['room'] for p in panels}:
            continue
        sh = HallShape.query.filter_by(kind='room', room_id=r.id).first()
        rooms[r.name] = {'building': r.building.name,
                         'shape': {k: getattr(sh, k) for k in ('x', 'z', 'width', 'depth', 'height', 'floors', 'elevation')} if sh else None}

    out = {
        'rooms': rooms,
        'panels': panels,
        'machine_panels': [{'machine': m.name, 'panel': nm(ElectricalPanel, m.panel_id)} for m in Machine.query if m.panel_id],
        'shelly': [{'host': s.host, 'panel': nm(ElectricalPanel, s.panel_id)} for s in ShellyDevice.query if s.panel_id],
        'modbus': [dict({'host': m.host, 'port': m.port}, **{c: nm(ElectricalPanel, getattr(m, c)) for c in MODBUS_PANEL_COLS}) for m in ModbusDevice.query],
        'connections': [dict(row(c, ('hall_machine_id', 'network_host_id', 'switch_device_id')), machine=c.hall_machine.name,
                             host_mac=c.network_host.mac_address if c.network_host else None, switch=c.switch_device.name if c.switch_device else None)
                        for c in MachineConnection.query],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

print(f"exported {len(panels)} panels, {sum(len(p['components']) for p in panels)} components, {sum(len(p['wires']) for p in panels)} wires, "
      f"{len(out['machine_panels'])} machine links, {len(out['shelly'])} shelly, {len(out['modbus'])} modbus, {len(out['connections'])} connections -> {OUT}")
