"""One-off seed: PressBrakeConfig of the shop's two press brakes (Durma AD-ES 2040 from the Polimeta offer 22/15042025/1, Durma AD-R 40175 from
the Durmazlar AD-R catalogue - check stroke/daylight against the machine plate). Machines are found by name; a missing one is reported, not
created. Existing values are not overwritten. Run: python -m migration.seed_press_brake_configs"""
from app import app, db, Machine, PressBrakeConfig

PRESSES = [
    ('AD-ES 2040', dict(working_length_mm=2050, tonnage_t=40, stroke_mm=200, daylight_mm=440, axes='X,R,Z1,Z2', controller='delem_da66t', clamping='European')),
    ('AD-R 40175', dict(working_length_mm=4050, tonnage_t=175, stroke_mm=265, daylight_mm=530, axes='Y1,Y2,X', controller='durma_dt15', clamping='European')),
]


def seed():
    done = 0
    for key, vals in PRESSES:
        m = Machine.query.filter(Machine.name.ilike(f'%{key}%')).first()
        if not m:
            print(f'Машина "{key}" не е намерена - добави я или използвай "Параметри на абкант".')
            continue
        cfg = m.press_brake or PressBrakeConfig(machine=m)
        for k, v in vals.items():
            if getattr(cfg, k) is None:
                setattr(cfg, k, v)
        db.session.add(cfg)
        done += 1
        print(f'{m.name}: готово')
    db.session.commit()
    return done


if __name__ == '__main__':
    with app.app_context():
        seed()
