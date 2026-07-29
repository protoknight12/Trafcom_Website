"""One-off: replaces the placeholder Machine rows ('CNC Machine #1', 'DMG MORI
DMU') with the real machine park listed on the public services page
(templates/services.html). Any Order/DxfFile pointing at a removed placeholder
is detached (machine_id set to NULL), same as delete_machine() in app.py -
orders/uploads are never deleted, just unassigned.

    python -m migration.seed_real_machines
"""
from app import app, db, Machine, Order, DxfFile

PLACEHOLDER_NAMES = ['CNC Machine #1', 'DMG MORI DMU']

REAL_MACHINES = [
    'DMG MORI DMU 75 monoBLOCK',
    'DMG MORI Milltap 700',
    'HURCO BMC 30',
    'HURCO BMC 4020 HT',
    'GILDEMEISTER TWIN 42',
    'BENZINGER TNI-B8',
    'BENZINGER TNI-B6',
    'DMG MORI CTX510 ecoline',
    'STAR KNC 32',
    'STAR KJR 16',
    'STAR SVR-20',
    'FIBER LASER ECKERT',
    'CSF 3015/700',
    'DURMA AD-R 40175',
    'DMG MORI UNO 20|40',
    'Brown & Sharpe Derby 454 (CMM)',
    'Центрофужна дискова машина TE18 W',
]

with app.app_context():
    for name in PLACEHOLDER_NAMES:
        placeholder = Machine.query.filter_by(name=name).first()
        if not placeholder:
            continue
        Order.query.filter_by(machine_id=placeholder.id).update({'machine_id': None})
        DxfFile.query.filter_by(machine_id=placeholder.id).update({'machine_id': None})
        db.session.delete(placeholder)
        print(f"Removed placeholder '{name}'")

    for name in REAL_MACHINES:
        if not Machine.query.filter_by(name=name).first():
            db.session.add(Machine(name=name))
            print(f"Added '{name}'")
        else:
            print(f"Already present: '{name}'")

    db.session.commit()
