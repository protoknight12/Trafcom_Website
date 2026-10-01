"""
One-off: applies the current hall layout from app.py (HALL_SEED / HALL_SHAPES_SEED) to an existing database:
  * machines listed in HALL_SEED_REMOVED are deleted (gone from this hall or moved to hall 2);
  * machines in HALL_SEED are repositioned by their number (names, heights, elevation and machine-card links are
    kept) or created when missing;
  * ALL walls/doors/rooms/fixtures/stairs/neighbour buildings are replaced by the seed (so room links to
    factory-map rooms have to be set again).
Anything you moved by hand in /admin/hall on those items is overwritten. Equipment is untouched.

    python -m migration.reset_hall_from_plan
"""
from app import (app, db, HallMachine, HallShape, HALL_SEED, HALL_SEED_REMOVED, HALL_SHAPES_SEED, HALL_LOOK_SEED,
                 HALL_ACC_SEED, _hall_shape_from_seed)

with app.app_context():
    db.create_all()
    deleted = HallMachine.query.filter(HallMachine.no.in_(HALL_SEED_REMOVED)).delete(synchronize_session=False)
    touched = 0
    for no, name, cat, x0, x1, z0, z1, h in HALL_SEED:
        m = HallMachine.query.filter_by(no=no).first() or HallMachine(no=no, name=name, category=cat, height=h)
        m.x, m.z, m.width, m.depth = x0, z0, round(x1 - x0, 2), round(z1 - z0, 2)
        m.model, m.rotation = HALL_LOOK_SEED.get(no, (m.model, m.rotation or 0))
        m.acc_length, m.acc_width, m.acc_height = HALL_ACC_SEED.get(no, (m.acc_length or 0.0, m.acc_width or 0.7, m.acc_height or 0.8))
        db.session.add(m)
        touched += 1
    HallShape.query.delete()
    db.session.add_all(_hall_shape_from_seed(t) for t in HALL_SHAPES_SEED)
    db.session.commit()

print(f"{deleted} machines removed, {touched} machines placed, {len(HALL_SHAPES_SEED)} plan shapes reseeded.")
