"""
One-off schema migration + data seed: creates the SolarPanelModel table (via
db.create_all()), adds modbus_device.panel_model_id (existing table, so a
raw ALTER TABLE), and seeds the two real module specs used on this roof -
Tongwei/TW Solar TWMND-72HD575 (inverter 1's side) and TWMND-72HD595
(inverter 2's side) - then links each to its Solis inverter (ordered by id,
matching every other "first inverter / second inverter" convention already
used across admin_solar_roof.html/admin_solar_roof_data()).

Sourced from public distributor pages (Synapsun's comparison table for the
electrical figures; dimensions/weight/cell count cross-checked against
Liriksolar and czpowersourcing) since the manufacturer's own PDF datasheet
returned 401 Unauthorized when fetched directly - see SolarPanelModel's
docstring in app.py for the full caveat on the electrical figures.

    python -m migration.seed_solar_panel_models

Safe to run more than once - skips seeding a model row if one with that
name already exists, and only sets an inverter's panel_model_id if it's
currently unset.
"""
from sqlalchemy import text

from app import app, db, ModbusDevice, SolarPanelModel

MODELS = [
    dict(
        name='TWMND-72HD575', manufacturer='Tongwei Solar (TW Solar)', rated_power_w=575,
        length_mm=2278, width_mm=1134, thickness_mm=30, weight_kg=31.5,
        cell_count=144, cell_type='N-type TOPCon monocrystalline half-cut (144 half-cells, 72-cell equivalent)',
        efficiency_pct=22.3, voc_v=51.44, isc_a=14.25, vmp_v=43.08, imp_a=13.35,
        temp_coeff_pmax_pct=-0.30, temp_coeff_voc_pct=-0.25, temp_coeff_isc_pct=0.046,
        max_system_voltage_v=1500, frame_material='Anodized aluminium alloy, grey',
        glass_type='Glass-glass, 2mm front + 2mm rear (bifacial)', junction_box='IP68, 3 diodes',
        notes='Dimensions/weight/cell count cross-confirmed (Synapsun, Liriksolar, czpowersourcing). '
              'Voc/Isc/Vmp/Imp/efficiency from Synapsun\'s comparison table - a second distributor page '
              '(Liriksolar) quoted slightly different Voc/Isc for this same 575W bin (52.10V/13.80A vs '
              '51.44V/14.25A here); normal for a power-binned product, treat as representative not exact. '
              'Manufacturer PDF (tongwei.cn) returned 401, could not cross-check directly.',
    ),
    dict(
        name='TWMND-72HD595', manufacturer='Tongwei Solar (TW Solar)', rated_power_w=595,
        length_mm=2278, width_mm=1134, thickness_mm=30, weight_kg=32.7,
        cell_count=144, cell_type='N-type TOPCon monocrystalline half-cut (144 half-cells, 72-cell equivalent)',
        efficiency_pct=23.03, voc_v=53.20, isc_a=14.15, vmp_v=44.41, imp_a=13.40,
        temp_coeff_pmax_pct=-0.30, temp_coeff_voc_pct=-0.25, temp_coeff_isc_pct=0.046,
        max_system_voltage_v=1500, frame_material='Anodized aluminium alloy, grey',
        glass_type='Glass-glass, 2mm front + 2mm rear (bifacial)', junction_box='IP68, 3 diodes',
        notes='Dimensions cross-confirmed (Synapsun, czpowersourcing). Electrical figures from Synapsun\'s '
              'comparison table only - no independent second source found for this specific 595W bin. '
              'Manufacturer PDF (tongwei.cn) returned 401, could not cross-check directly.',
    ),
]

with app.app_context():
    db.create_all()
    db.session.execute(text('''
        ALTER TABLE modbus_device ADD COLUMN IF NOT EXISTS panel_model_id INTEGER
        REFERENCES solar_panel_model(id)
    '''))
    db.session.commit()

    name_to_id = {}
    for spec in MODELS:
        existing = SolarPanelModel.query.filter_by(name=spec['name']).first()
        if existing:
            print(f"{spec['name']}: already seeded, skipping.")
            name_to_id[spec['name']] = existing.id
            continue
        row = SolarPanelModel(**spec)
        db.session.add(row)
        db.session.flush()
        name_to_id[spec['name']] = row.id
        print(f"{spec['name']}: seeded (id={row.id}).")
    db.session.commit()

    inverters = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all()
    if len(inverters) != 2:
        print(f"Expected 2 Solis inverters, found {len(inverters)} - not linking panel models.")
    else:
        pairing = [(inverters[0], 'TWMND-72HD575'), (inverters[1], 'TWMND-72HD595')]
        for inv, model_name in pairing:
            if inv.panel_model_id:
                print(f"{inv.name}: panel_model_id already set, leaving as-is.")
                continue
            inv.panel_model_id = name_to_id[model_name]
            print(f"{inv.name}: linked to {model_name}.")
        db.session.commit()

print("Done.")
