"""
One-off data seed: creates the SolarPanel table (if missing, via
db.create_all()) and populates it with 172 rows for this shop's real
56m x 13m roof - 86 modules per slope, one slope per Solis inverter, 5
rows each (4 rows of 17 + 1 row of 18, since 86 isn't evenly divisible
by 5 - the extra module goes in the last row).

    python -m migration.seed_solar_roof

Safe to run more than once - skips any inverter that already has panels
seeded, so it won't duplicate rows or wipe existing string assignments.
"""
from app import app, db, ModbusDevice, SolarPanel

ROWS = 5
PANELS_PER_SIDE = 86


def row_counts(total, rows):
    base = total // rows
    extra = total - base * rows
    counts = [base] * rows
    for i in range(extra):
        counts[rows - 1 - i] += 1
    return counts


with app.app_context():
    db.create_all()
    inverters = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all()
    if len(inverters) != 2:
        print(f"Expected 2 Solis inverters, found {len(inverters)} - aborting, nothing seeded.")
    else:
        counts = row_counts(PANELS_PER_SIDE, ROWS)
        created = 0
        for inv in inverters:
            if SolarPanel.query.filter_by(inverter_device_id=inv.id).first():
                print(f"{inv.name}: already has panels seeded, skipping.")
                continue
            for row_idx, count in enumerate(counts, start=1):
                for col_idx in range(1, count + 1):
                    db.session.add(SolarPanel(inverter_device_id=inv.id, row=row_idx, col=col_idx))
                    created += 1
        db.session.commit()
        print(f"Created {created} solar panel rows (rows per side: {counts}).")
