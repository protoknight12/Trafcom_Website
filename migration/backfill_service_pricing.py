"""
One-off: seeds the Service catalog (billable operation types + EUR/hour rates)
shown on /admin/services, from a snapshot taken off a working dev database.

Service isn't reproducible from a constant in app.py the way
SERVICE_MACHINE_CARDS_SEED is - it's hand-entered pricing, so this script
carries a literal snapshot instead of pulling from app.py.

Safe to run more than once - matches existing rows by name and skips them
rather than creating duplicates. Machines are looked up by name (must already
exist - run migration.seed_real_machines first if starting from empty).

    python -m migration.backfill_service_pricing
"""
from app import app, db, Service, Machine

SERVICES = [
    {
        'name': 'Лазерно рязане',
        'machine_type': 'laser',
        'price_per_hour_eur': 50.0,
        'machines': ['FIBER LASER ECKERT', 'CSF 3015/700'],
    },
    {
        'name': 'Фрезоване - 5 оси',
        'machine_type': 'mill_5axis',
        'price_per_hour_eur': 70.0,
        'machines': ['DMG MORI DMU 75 monoBLOCK', 'DMG MORI Milltap 700'],
    },
    {
        'name': 'Фрезоване - 3 оси',
        'machine_type': 'mill_3axis',
        'price_per_hour_eur': 50.0,
        'machines': ['HURCO BMC 30', 'HURCO BMC 4020 HT'],
    },
    {
        'name': 'Струговане',
        'machine_type': 'lathe',
        'price_per_hour_eur': 45.0,
        'machines': ['GILDEMEISTER TWIN 42', 'BENZINGER TNI-B8', 'BENZINGER TNI-B6', 'DMG MORI CTX510 ecoline'],
    },
    {
        'name': 'Swiss-type струговане',
        'machine_type': 'lathe_swiss',
        'price_per_hour_eur': 40.0,
        'machines': ['STAR KNC 32', 'STAR KJR 16', 'STAR SVR-20'],
    },
    {
        'name': 'Огъване',
        'machine_type': 'bending',
        'price_per_hour_eur': 35.0,
        'machines': ['DURMA AD-R 40175'],
    },
    {
        'name': '3D координатно измерване',
        'machine_type': 'measuring_cmm',
        'price_per_hour_eur': 30.0,
        'machines': ['Brown & Sharpe Derby 454 (CMM)'],
    },
    {
        'name': 'Полиране / Довършителна обработка',
        'machine_type': 'finishing',
        'price_per_hour_eur': 25.0,
        'machines': ['Центрофужна дискова машина TE18 W'],
    },
]

with app.app_context():
    for entry in SERVICES:
        if Service.query.filter_by(name=entry['name']).first():
            print(f"Already present: '{entry['name']}'")
            continue
        machines = Machine.query.filter(Machine.name.in_(entry['machines'])).all()
        found_names = {m.name for m in machines}
        missing = set(entry['machines']) - found_names
        if missing:
            print(f"WARNING: '{entry['name']}' - machine(s) not found, skipping link: {missing}")
        db.session.add(Service(
            name=entry['name'],
            machine_type=entry['machine_type'],
            price_per_hour_eur=entry['price_per_hour_eur'],
            machines=machines,
        ))
        print(f"Added '{entry['name']}' ({entry['price_per_hour_eur']} EUR/h, {len(machines)} machine(s) linked)")

    db.session.commit()
