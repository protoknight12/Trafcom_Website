"""
Carries the machine/card data edited locally over to production (ids differ between databases, so rows are matched by name):
ServiceMachineCard (page+kind+title), Machine (name; its room/panel by name), HallMachine (name; linked Machine/card by name),
MachineConnection (per hall machine, label; its NetworkHost by MAC and switch by name when they exist on production), Room (name; building by name), ElectricalPanel (name; room/parent by name, map positions),
HallShape + HallEquipment (the hall plan: production's rows are REPLACED by the local ones), HallParcel (КАИС boundaries, by cadastral number)
and the hierarchy ПИ -> Сграда -> Помещение -> objects (parent_id / parcel_id / building_id are ids, so they travel as a position in the shape list,
a cadastral number and a Building name), and the room/map position of existing
Convector / TemperatureSensor / BatteryStack / NetworkDevice rows (matched by name, never created).

    python -m migration.machine_data_sync export     # on the local PC  -> migration/machine_data.json
    python -m migration.machine_data_sync import     # on the server    <- the same file (after git pull / upload)

Import creates what is missing and OVERWRITES the listed columns of what exists (local is the source of truth); nothing is deleted.
Not carried: HallMachineFile bytes (copy machine_files/ by hand),
card images (copy static/uploads/ files by hand - the file name is carried, the file itself is not).
"""
import json
import os
import sys
from datetime import datetime

from app import (app, db, Machine, ServiceMachineCard, HallMachine, MachineConnection, Building, Room, ElectricalPanel, HallShape,
                 HallEquipment, HallParcel, NetworkHost, Convector, TemperatureSensor, BatteryStack, NetworkDevice, ModbusDevice, _hall_parcels)

PATH = os.path.join(os.path.dirname(__file__), 'machine_data.json')
SKIP = {'id', 'parent_id', 'parcel_id', 'building_id'}     # ids that differ between databases - exported as stable keys instead (see parent_key)


def cols(model):
    """Plain data columns: no pk, no foreign keys."""
    return [c.name for c in model.__table__.columns if c.name not in SKIP and not c.foreign_keys]


def dump(obj, model):
    out = {}
    for c in cols(model):
        v = getattr(obj, c)
        out[c] = v.isoformat() if isinstance(v, datetime) else v
    return out


def load(obj, model, data):
    for c in cols(model):
        if c in data:
            v = data[c]
            if v is not None and isinstance(model.__table__.columns[c].type, db.DateTime):
                v = datetime.fromisoformat(v)
            setattr(obj, c, v)


def card_key(c):
    return {'page': c.page, 'kind': c.kind, 'title': c.title}


def find_card(k):
    return ServiceMachineCard.query.filter_by(page=k['page'], kind=k['kind'], title=k['title']).first() if k else None


EQUIP = {'panel': ElectricalPanel, 'convector': Convector, 'sensor': TemperatureSensor, 'battery': BatteryStack,
         'network': NetworkDevice, 'inverter': ModbusDevice}
PLACED = (Convector, TemperatureSensor, BatteryStack)            # room + position on the room map, updated by name
name_of = lambda o: o.name if o else None


def by_name(model, name):
    return model.query.filter_by(name=name).first() if name else None


def parent_key(pid, idx):
    """HallShape.parent_id -> None (by position) / 'hall' (0 = main hall) / the shape's position in the exported list."""
    return None if pid is None else 'hall' if pid == 0 else idx.get(pid)


def parent_id(key, new_ids):
    return None if key is None else 0 if key == 'hall' else new_ids[key]


def export():
    _hall_parcels()                                          # makes sure the КАИС boundaries exist locally
    db.session.commit()
    idx = {s.id: i for i, s in enumerate(HallShape.query.order_by(HallShape.id))}
    data = {
        'parcels': [{'cadnum': p.cadnum, 'area': p.area, 'points_json': p.points_json} for p in HallParcel.query.order_by(HallParcel.id)],
        'cards': [dump(c, ServiceMachineCard) for c in ServiceMachineCard.query.order_by(ServiceMachineCard.id)],
        'machines': [dict(dump(m, Machine), _room=name_of(m.room), _panel=name_of(m.panel)) for m in Machine.query.order_by(Machine.id)],
        'rooms': [dict(dump(r, Room), _building=name_of(Building.query.get(r.building_id))) for r in Room.query.order_by(Room.id)],
        'panels': [dict(dump(p, ElectricalPanel), _room=name_of(p.room), _parent=name_of(p.parent_panel)) for p in ElectricalPanel.query.order_by(ElectricalPanel.id)],
        'shapes': [dict(dump(s, HallShape), _room=name_of(Room.query.get(s.room_id)), _parent=parent_key(s.parent_id, idx),
                        _parcel=getattr(db.session.get(HallParcel, s.parcel_id), 'cadnum', None) if s.parcel_id else None,
                        _building=name_of(db.session.get(Building, s.building_id)) if s.building_id else None)
                   for s in HallShape.query.order_by(HallShape.id)],
        'equipment': [dict(dump(e, HallEquipment), _parent=parent_key(e.parent_id, idx),
                           _ref=name_of(EQUIP[e.kind].query.get(e.ref_id)) if e.kind in EQUIP and e.ref_id else None)
                      for e in HallEquipment.query.order_by(HallEquipment.id)],
        'placed': [dict(dump(o, M), _model=M.__name__, _room=name_of(o.room)) for M in PLACED for o in M.query],
        'network_pos': [{'name': d.name, 'pos_x': d.pos_x, 'pos_y': d.pos_y} for d in NetworkDevice.query],
        'hall': [],
    }
    for h in HallMachine.query.order_by(HallMachine.id):
        d = dump(h, HallMachine)
        d['_parent'] = parent_key(h.parent_id, idx)
        d['_machine'] = h.machine.name if h.machine_id and h.machine else None
        d['_card'] = card_key(h.card) if h.card_id and h.card else None
        d['_connections'] = [dict(dump(c, MachineConnection), _host=c.network_host.mac_address if c.network_host else None,
                                  _switch=name_of(NetworkDevice.query.get(c.switch_device_id))) for c in h.connections]
        data['hall'].append(d)
    with open(PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"exported {len(data['cards'])} cards, {len(data['machines'])} machines, {len(data['hall'])} hall machines -> {PATH}")


def upsert(model, row, **keys):
    obj = model.query.filter_by(**keys).first()
    if not obj:
        obj = model(**keys)
        db.session.add(obj)
    load(obj, model, row)
    return obj


def import_():
    with open(PATH, encoding='utf-8') as f:
        data = json.load(f)
    for r in data.get('parcels', []):
        upsert(HallParcel, r, cadnum=r['cadnum'])
    for r in data['cards']:
        upsert(ServiceMachineCard, r, page=r['page'], kind=r['kind'], title=r['title'])
    for r in data['rooms']:
        b = by_name(Building, r['_building']) or Building(name=r['_building'])
        db.session.add(b)
        db.session.flush()
        upsert(Room, r, name=r['name']).building_id = b.id
    db.session.flush()
    for r in data['panels']:
        p = upsert(ElectricalPanel, r, name=r['name'])
        p.room_id = by_name(Room, r['_room']).id
    db.session.flush()
    for r in data['panels']:
        if r['_parent']:
            by_name(ElectricalPanel, r['name']).parent_panel_id = by_name(ElectricalPanel, r['_parent']).id
    for r in data['machines']:
        m = upsert(Machine, r, name=r['name'])
        m.room_id = getattr(by_name(Room, r['_room']), 'id', None)
        m.panel_id = getattr(by_name(ElectricalPanel, r['_panel']), 'id', None)
    for M in PLACED:
        for r in (x for x in data['placed'] if x['_model'] == M.__name__):
            o = by_name(M, r['name'])
            if o:
                o.room_id = getattr(by_name(Room, r['_room']), 'id', None)
                if 'pos_x' in r:                                            # sensors have a room but no map position
                    o.pos_x, o.pos_y = r['pos_x'], r['pos_y']
    for r in data['network_pos']:
        d = by_name(NetworkDevice, r['name'])
        if d:
            d.pos_x, d.pos_y = r['pos_x'], r['pos_y']
    HallShape.query.delete()
    HallEquipment.query.delete()
    db.session.flush()
    made = []
    for r in data['shapes']:
        s = HallShape()
        load(s, HallShape, r)
        s.room_id = getattr(by_name(Room, r['_room']), 'id', None)
        db.session.add(s)
        made.append((s, r))
    db.session.flush()
    new_ids = [s.id for s, _ in made]                            # position in the list -> id on this database
    for s, r in made:
        s.parent_id = parent_id(r.get('_parent'), new_ids)
        parcel = HallParcel.query.filter_by(cadnum=r['_parcel']).first() if r.get('_parcel') else None
        s.parcel_id = parcel.id if parcel else None
        if r.get('_building'):                                    # a drawn building keeps its Building row (created when missing)
            b = by_name(Building, r['_building']) or Building(name=r['_building'])
            db.session.add(b)
            db.session.flush()
            s.building_id = b.id
    for r in data['equipment']:
        e = HallEquipment()
        load(e, HallEquipment, r)
        e.parent_id = parent_id(r.get('_parent'), new_ids)
        e.ref_id = getattr(by_name(EQUIP[r['kind']], r['_ref']), 'id', None) if r['kind'] in EQUIP else None
        db.session.add(e)
    db.session.flush()
    for r in data['hall']:
        h = upsert(HallMachine, r, name=r['name'])
        h.parent_id = parent_id(r.get('_parent'), new_ids)
        db.session.flush()
        m = Machine.query.filter_by(name=r['_machine']).first() if r['_machine'] else None
        if m and (not m.hall_record or m.hall_record is h):     # machine_id is unique: never steal another record's machine
            h.machine_id = m.id
        card = find_card(r['_card'])
        if card:
            h.card_id = card.id
        for c in r['_connections']:
            mc = upsert(MachineConnection, c, hall_machine_id=h.id, label=c['label'])
            host = NetworkHost.query.filter_by(mac_address=c['_host']).first() if c['_host'] else None   # a host is matched by MAC, never created
            mc.network_host_id = host.id if host else None
            mc.switch_device_id = getattr(by_name(NetworkDevice, c['_switch']), 'id', None)
    db.session.commit()
    print(f"imported {len(data['cards'])} cards, {len(data['machines'])} machines, {len(data['hall'])} hall machines")


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode not in ('export', 'import'):
        sys.exit(__doc__)
    with app.app_context():
        export() if mode == 'export' else import_()
