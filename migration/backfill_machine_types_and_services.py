"""
One-off data backfill: categorizes the existing real Machine catalog by
machine_type and creates a starter Service per category, linked to the
matching machines (see Service.machines / service_machine in app.py).

Matches machines by name against MACHINE_TYPE_BY_NAME below - the shop's
machine names are stable identifiers already used throughout the DB (Order/
DxfFile history, ServiceMachineCard specs), so name matching is simpler than
introducing a separate machine-id mapping just for this one-off script.

Safe to re-run:
- machine_type is only set on a Machine that doesn't already have one - never
  clobbers an admin's own edit (same rule as every other backfill script here).
- A Service is only created if no Service with that exact name already
  exists - an admin's edits to an existing service (price, description,
  machine links) are left alone.

price_per_hour_eur values below are placeholder shop-rate estimates, not
measured real numbers - same caveat as DEFAULT_MATERIAL_SEED's cutting
speeds. Retune via /admin/services.

    python -m migration.backfill_machine_types_and_services
"""
from app import app, db, Machine, Service

# name -> machine_type category (see Machine.machine_type / Service.machine_type)
MACHINE_TYPE_BY_NAME = {
    'DMG MORI DMU 75 monoBLOCK': 'mill_5axis',
    'DMG MORI Milltap 700': 'mill_5axis',
    'HURCO BMC 30': 'mill_3axis',
    'HURCO BMC 4020 HT': 'mill_3axis',
    'GILDEMEISTER TWIN 42': 'lathe',
    'BENZINGER TNI-B8': 'lathe',
    'BENZINGER TNI-B6': 'lathe',
    'DMG MORI CTX510 ecoline': 'lathe',
    'STAR KNC 32': 'lathe_swiss',
    'STAR KJR 16': 'lathe_swiss',
    'STAR SVR-20': 'lathe_swiss',
    'FIBER LASER ECKERT': 'laser',
    'CSF 3015/700': 'laser',
    'DURMA AD-R 40175': 'bending',
    'DMG MORI UNO 20|40': 'tool_presetting',
    'Brown & Sharpe Derby 454 (CMM)': 'measuring_cmm',
    'Центрофужна дискова машина TE18 W': 'finishing',
}

# machine_type -> (service name, EUR/hour placeholder rate). tool_presetting
# has no entry - a tool presetter is an internal setup aid, not a
# customer-billable operation, so it gets a machine_type for grouping but no
# Service of its own.
SERVICE_BY_TYPE = {
    'laser': ('Лазерно рязане', 50.0),
    'mill_5axis': ('Фрезоване - 5 оси', 70.0),
    'mill_3axis': ('Фрезоване - 3 оси', 50.0),
    'lathe': ('Струговане', 45.0),
    'lathe_swiss': ('Swiss-type струговане', 40.0),
    'bending': ('Огъване', 35.0),
    'measuring_cmm': ('3D координатно измерване', 30.0),
    'finishing': ('Полиране / Довършителна обработка', 25.0),
}

with app.app_context():
    machines_by_type = {}
    types_set = 0
    for machine in Machine.query.all():
        machine_type = MACHINE_TYPE_BY_NAME.get(machine.name)
        if not machine_type:
            continue
        machines_by_type.setdefault(machine_type, []).append(machine)
        if not machine.machine_type:
            machine.machine_type = machine_type
            types_set += 1
    db.session.commit()

    services_created = 0
    services_linked = 0
    for machine_type, (service_name, price_per_hour_eur) in SERVICE_BY_TYPE.items():
        matched_machines = machines_by_type.get(machine_type, [])
        existing = Service.query.filter_by(name=service_name).first()
        if existing:
            # Backfill machine links onto a pre-existing service (e.g. the
            # 'Лазерно рязане' row seed_billable_services() already creates)
            # only if nothing has been linked yet - never override an
            # admin's own machine selection.
            if not existing.machines and matched_machines:
                existing.machines = matched_machines
                services_linked += 1
            continue
        db.session.add(Service(
            name=service_name, machine_type=machine_type, price_per_hour_eur=price_per_hour_eur,
            machines=matched_machines,
        ))
        services_created += 1
    db.session.commit()

print(f"machine_type set on {types_set} machine(s), {services_created} service(s) created, "
      f"{services_linked} pre-existing service(s) linked to machines.")
