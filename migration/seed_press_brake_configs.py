"""One-off seed: PressBrakeConfig of the shop's two press brakes (Durma AD-ES 2040 from the Polimeta offer 22/15042025/1, Durma AD-R 40175 from
the Durmazlar AD-R catalogue - check stroke/daylight against the machine plate). Machines are found by name; a missing one is reported, not
created. Existing values are not overwritten. Run: python -m migration.seed_press_brake_configs"""
import json

from app import app, db, Machine, PressBrakeConfig

# From the controller's own machine parameters (DA-69S 4.2.16, Polimeta order 73052521882): axis limits in mm, finger 85 mm wide with two levels (14 / 10 mm)
AD_ES_KINEMATICS = {'controller': 'Delem DA-69S 4.2.16', 'y': [20, 220], 'x': [0, 650], 'r': [-45, 205], 'z1': [400, 1560], 'z2': [490, 1650], 'crowning': 'motor 0-100',
                    'between_frames_mm': 1730, 'tool_reference_mm': 338, 'x_speed_mm_s': 600, 'finger_width': 85, 'finger_levels_mm': [14, 10],
                    'r_before_x_mm': 5, 'rapid_mm_s': [100, 150], 'work_mm_s': [10, 20],
                    'frame': {   # side plate traced from the body drawing (Durma 730502001, isometric, scale fixed by the 1730 mm between the frames): (a, b) = mm from the
                              # plate's back edge and from its bottom edge; the die's centre line is at a_die, its top plane at b_die; plate thickness ~100 (1830 centre to centre)
                'a_die': 1190, 'b_die': 1010, 'thickness': 100,
                'profile': [[0, 0], [1300, 0], [1300, 600], [1020, 600], [975, 625], [940, 660], [906, 700], [860, 800], [811, 932], [811, 1500], [820, 1550],
                            [845, 1600], [890, 1635], [960, 1650], [1200, 1650], [1200, 2650], [0, 2650]]},
                    'overall_mm': [2600, 1450, 2650], 'work_height_mm': 1000, 'die_holder_mm': [550, 1050, 450], 'clamps': 10, 'z_min_gap_mm': 90}

PRESSES = [
    ('AD-ES 2040', dict(working_length_mm=2050, tonnage_t=40, stroke_mm=200, daylight_mm=440, axes='Y1,Y2,X,R,Z1,Z2,CROWN', x_max_mm=650, r_max_mm=250, finger_count=2, controller='delem_da69s', kinematics_json=json.dumps(AD_ES_KINEMATICS), clamping='European')),
    ('AD-R 40175', dict(working_length_mm=4050, tonnage_t=175, stroke_mm=265, daylight_mm=530, axes='Y1,Y2,X', finger_count=2, controller='durma_dt15', clamping='European')),
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
            if k == 'kinematics_json':   # merge: keeps the finger outlines imported by import_delem_tools
                v = json.dumps({**cfg.kinematics, **json.loads(v)})
            if getattr(cfg, k) is None or (key == 'AD-ES 2040' and k in ('axes', 'controller', 'kinematics_json')):   # the earlier guess (X,R,Z1,Z2 / DA-66T) is replaced by the controller's own data
                setattr(cfg, k, v)
        db.session.add(cfg)
        done += 1
        print(f'{m.name}: готово')
    db.session.commit()
    return done


if __name__ == '__main__':
    with app.app_context():
        seed()
