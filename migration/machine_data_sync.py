"""
Carries the machine/card data edited locally over to production (ids differ between databases, so rows are matched by name):
ServiceMachineCard (page+kind+title), Machine (name), HallMachine (name; linked Machine/card by name), MachineConnection (per hall machine, label).

    python -m migration.machine_data_sync export     # on the local PC  -> migration/machine_data.json
    python -m migration.machine_data_sync import     # on the server    <- the same file (after git pull / upload)

Import creates what is missing and OVERWRITES the listed columns of what exists (local is the source of truth); nothing is deleted.
Not carried: foreign keys to rooms/panels/network (ids differ), HallMachineFile bytes (copy machine_files/ by hand),
card images (copy static/uploads/ files by hand - the file name is carried, the file itself is not).
"""
import json
import os
import sys
from datetime import datetime

from app import app, db, Machine, ServiceMachineCard, HallMachine, MachineConnection

PATH = os.path.join(os.path.dirname(__file__), 'machine_data.json')
SKIP = {'id'}


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


def export():
    data = {
        'cards': [dump(c, ServiceMachineCard) for c in ServiceMachineCard.query.order_by(ServiceMachineCard.id)],
        'machines': [dump(m, Machine) for m in Machine.query.order_by(Machine.id)],
        'hall': [],
    }
    for h in HallMachine.query.order_by(HallMachine.id):
        d = dump(h, HallMachine)
        d['_machine'] = h.machine.name if h.machine_id and h.machine else None
        d['_card'] = card_key(h.card) if h.card_id and h.card else None
        d['_connections'] = [dump(c, MachineConnection) for c in h.connections]
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
    for r in data['cards']:
        upsert(ServiceMachineCard, r, page=r['page'], kind=r['kind'], title=r['title'])
    for r in data['machines']:
        upsert(Machine, r, name=r['name'])
    db.session.flush()
    for r in data['hall']:
        h = upsert(HallMachine, r, name=r['name'])
        db.session.flush()
        m = Machine.query.filter_by(name=r['_machine']).first() if r['_machine'] else None
        if m and (not m.hall_record or m.hall_record is h):     # machine_id is unique: never steal another record's machine
            h.machine_id = m.id
        card = find_card(r['_card'])
        if card:
            h.card_id = card.id
        for c in r['_connections']:
            upsert(MachineConnection, c, hall_machine_id=h.id, label=c['label'])
    db.session.commit()
    print(f"imported {len(data['cards'])} cards, {len(data['machines'])} machines, {len(data['hall'])} hall machines")


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode not in ('export', 'import'):
        sys.exit(__doc__)
    with app.app_context():
        export() if mode == 'export' else import_()
