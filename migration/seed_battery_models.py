"""
One-off schema migration + data seed: creates the BatteryModel table (via
db.create_all()), adds battery.model_id (existing table, so a raw ALTER
TABLE), and seeds the one real battery module used in this shop's stacks -
Dyness S51100 - so the "add battery" form (admin_stack_batteries.html) can
offer it in a dropdown instead of manual voltage/capacity entry.

Sourced from public distributor pages (KaMeaSolar, Rebor, CCL Solar, ONSA
Plus, Jardis, LirikSolar) since Dyness's own datasheet PDF wasn't directly
fetchable - see BatteryModel's docstring in app.py for the two minor
figures (cycle_life, protection_rating) where sources disagreed.

    python -m migration.seed_battery_models

Safe to run more than once - skips seeding if a model with that name
already exists.
"""
from sqlalchemy import text

from app import app, db, BatteryModel

MODELS = [
    dict(
        name='Dyness S51100', manufacturer='Dyness', chemistry='LiFePO4',
        nominal_voltage_v=51.2, capacity_ah=100, energy_kwh=5.12, usable_energy_kwh=4.864,
        continuous_current_a=100, max_discharge_power_kw=5.12, round_trip_efficiency_pct=95,
        cycle_life=8000, length_mm=657, width_mm=460, height_mm=292, weight_kg=45,
        protection_rating='IP66', communication='CAN / RS485',
        notes='Core figures (51.2V/100Ah/5.12kWh, 100A continuous, 657x460x292mm, 45kg, CAN/RS485) '
              'cross-confirmed across KaMeaSolar, Rebor, CCL Solar, ONSA Plus, Jardis.lt and LirikSolar '
              'distributor listings - Dyness\'s own datasheet PDF could not be fetched directly. '
              'cycle_life: KaMeaSolar and Dyness\'s own indexed site text both say >=8000 cycles; CCL Solar '
              'said 6000 - kept 8000 as the manufacturer figure. protection_rating: KaMeaSolar says IP66; '
              'CCL Solar says IP65 - kept IP66, matching the majority/manufacturer-adjacent source. '
              'usable_energy_kwh (4.864kWh, ~95% of nominal) from Jardis.lt.',
    ),
]

with app.app_context():
    db.create_all()
    db.session.execute(text('''
        ALTER TABLE battery ADD COLUMN IF NOT EXISTS model_id INTEGER
        REFERENCES battery_model(id)
    '''))
    db.session.commit()

    for spec in MODELS:
        existing = BatteryModel.query.filter_by(name=spec['name']).first()
        if existing:
            print(f"{spec['name']}: already seeded, skipping.")
            continue
        db.session.add(BatteryModel(**spec))
        print(f"{spec['name']}: seeded.")
    db.session.commit()

print("Done.")
