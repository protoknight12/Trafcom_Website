"""
One-time data step (run by migration/run_once.py): loads migration/data/panels_export.json (made by migration/export_panels.py on the
development database) into this one. Everything is matched by NAME and nothing is ever deleted or overwritten:
  - a panel (by name) that exists is only completed where its fields are empty; a missing one is created in the room of the same name.
    Its schematic (components + wires) is loaded only when the panel has no components yet;
  - machine / Shelly / Modbus -> panel links are set only where the target has no panel yet;
  - machine communications are added when an identical one (machine, kind, label, address) is not there yet.
Anything that cannot be matched (unknown room, machine, host) is skipped and listed at the end.

    python -m migration.data_import_panels
"""
import json
import os

from app import (app, db, ElectricalPanel, PanelComponent, PanelWire, Machine, ShellyDevice, ModbusDevice, MachineConnection, HallMachine,
                 NetworkHost, NetworkDevice, Room)

SRC = os.path.join(os.path.dirname(__file__), 'data', 'panels_export.json')
MODBUS_PANEL_COLS = ('panel_id', 'grid_panel_id', 'main_panel_id', 'backup_panel_id', 'generator_panel_id')
skipped, stats = [], {'panels': 0, 'components': 0, 'wires': 0, 'links': 0, 'connections': 0}


def fill(obj, values):
    """Sets only the attributes that are still empty."""
    for k, v in values.items():
        if v is not None and getattr(obj, k, None) in (None, ''):
            setattr(obj, k, v)


def machine(name):
    return Machine.query.filter_by(name=name).first() if name else None


with app.app_context():
    with open(SRC, encoding='utf-8') as f:
        data = json.load(f)
    panel_by_name = {}                                                    # name -> panel (the first one wins)
    for p in ElectricalPanel.query.order_by(ElectricalPanel.id):
        panel_by_name.setdefault(p.name, p)

    # 1. panels
    fresh = {}                                                            # name -> True when its schematic should be loaded
    for d in data['panels']:
        panel = panel_by_name.get(d['name'])
        values = {k: v for k, v in d.items() if k not in ('components', 'wires', 'room', 'parent')}
        if panel is None:
            room = Room.query.filter_by(name=d['room']).first() if d['room'] else None
            if room is None:
                skipped.append(f"panel {d['name']}: room {d['room']!r} not found")
                continue
            panel = ElectricalPanel(room_id=room.id, **values)
            db.session.add(panel)
            db.session.flush()
            stats['panels'] += 1
        else:
            fill(panel, values)
        panel_by_name[d['name']] = panel
        fresh[d['name']] = not panel.components

    # 2. parent links (every panel exists now)
    for d in data['panels']:
        panel = panel_by_name.get(d['name'])
        if panel and d['parent'] and panel.parent_panel_id is None and d['parent'] in panel_by_name:
            panel.parent_panel_id = panel_by_name[d['parent']].id
    db.session.flush()

    # 3. schematics (component ids are kept across panels: a wire can end on a component of another panel)
    comp_id, new_panels = {}, []
    for d in data['panels']:
        panel = panel_by_name.get(d['name'])
        if panel is None or not fresh.get(d['name']):
            continue
        new_panels.append((d, panel))
        for c in d['components']:
            values = {k: v for k, v in c.items() if k not in ('ref', 'feeds_machine', 'feeds_panel', 'feeds_modbus')}
            fm = machine(c['feeds_machine'])
            fp = panel_by_name.get(c['feeds_panel'])
            md = ModbusDevice.query.filter_by(host=c['feeds_modbus']).first() if c['feeds_modbus'] else None
            comp = PanelComponent(panel_id=panel.id, feeds_machine_id=fm.id if fm else None, feeds_panel_id=fp.id if fp else None,
                                  feeds_modbus_device_id=md.id if md else None, **values)
            db.session.add(comp)
            db.session.flush()
            comp_id[c['ref']] = comp.id
            stats['components'] += 1
    for d, panel in new_panels:
        for w in d['wires']:
            if w['frm'] in comp_id and w['to'] in comp_id:
                values = {k: v for k, v in w.items() if k not in ('frm', 'to')}
                db.session.add(PanelWire(panel_id=panel.id, from_component_id=comp_id[w['frm']], to_component_id=comp_id[w['to']], **values))
                stats['wires'] += 1

    # 4. links of machines and meters to panels (only where empty)
    for l in data['machine_panels']:
        m, p = machine(l['machine']), panel_by_name.get(l['panel'])
        if m is None or p is None:
            skipped.append(f"machine {l['machine']!r} -> panel {l['panel']!r}")
        elif m.panel_id is None:
            m.panel_id = p.id
            stats['links'] += 1
    for l in data['shelly']:
        s, p = ShellyDevice.query.filter_by(host=l['host']).first(), panel_by_name.get(l['panel'])
        if s is None or p is None:
            skipped.append(f"shelly {l['host']} -> panel {l['panel']!r}")
        elif s.panel_id is None:
            s.panel_id = p.id
            stats['links'] += 1
    for l in data['modbus']:
        md = ModbusDevice.query.filter_by(host=l['host'], port=l['port']).first()
        if md is None:
            skipped.append(f"modbus {l['host']}:{l['port']}")
            continue
        for col in MODBUS_PANEL_COLS:
            p = panel_by_name.get(l[col]) if l[col] else None
            if p is not None and getattr(md, col) is None:
                setattr(md, col, p.id)
                stats['links'] += 1

    # 5. machine communications
    for c in data['connections']:
        hm = HallMachine.query.filter_by(name=c['machine']).first()
        if hm is None:
            skipped.append(f"connection of {c['machine']!r}: no dossier")
            continue
        if MachineConnection.query.filter_by(hall_machine_id=hm.id, kind=c['kind'], label=c['label'], address=c['address']).first():
            continue
        host = NetworkHost.query.filter_by(mac_address=c['host_mac']).first() if c['host_mac'] else None
        sw = NetworkDevice.query.filter_by(name=c['switch']).first() if c['switch'] else None
        values = {k: v for k, v in c.items() if k not in ('machine', 'host_mac', 'switch')}
        db.session.add(MachineConnection(hall_machine_id=hm.id, network_host_id=host.id if host else None,
                                         switch_device_id=sw.id if sw else None, **values))
        stats['connections'] += 1

    db.session.commit()

print('imported:', stats)
for x in skipped:
    print('  skipped:', x)
