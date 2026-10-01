"""
One-off: re-applies the hall layout read from the architect's plan (HALL_SEED / HALL_SHAPES_SEED in
app.py) to an existing database - machine positions/sizes are updated by their plan number (names,
heights, elevation and machine-card links are kept), and ALL walls/doors/rooms/fixtures/stairs/
neighbour buildings are replaced by the seed (so room links to factory-map rooms have to be set again).
Anything you moved by hand in /admin/hall on those items is overwritten. Equipment (inverters, batteries,
panels) is untouched.

    python -m migration.reset_hall_from_plan
"""
from app import app, db, HallMachine, HallShape, HALL_SEED, HALL_SHAPES_SEED, _hall_shape_from_seed

with app.app_context():
    db.create_all()
    updated = 0
    for no, name, cat, x0, x1, z0, z1, h in HALL_SEED:
        for m in HallMachine.query.filter_by(no=no):
            m.x, m.z, m.width, m.depth = x0, z0, round(x1 - x0, 2), round(z1 - z0, 2)
            updated += 1
    HallShape.query.delete()
    db.session.add_all(_hall_shape_from_seed(t) for t in HALL_SHAPES_SEED)
    db.session.commit()

print(f"{updated} machines repositioned, {len(HALL_SHAPES_SEED)} plan shapes reseeded.")
