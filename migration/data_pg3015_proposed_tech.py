"""
One-time data step (owner, 2026-11-24): proposed cutting technologies for the laser "PG3015" (6 kW source) for every
standard thickness inside its thickness range that has no technology yet - steel, stainless, aluminium, brass,
galvanized, plus copper up to 6 mm. All rows are needs_test=True: shown "За тест" in the dossier until the owner presses
"Работи ✓" after a test cut.

How the values were made: speed between two of the machine's own recorded thicknesses = geometric interpolation, then
checked against at least two published 6 kW charts (Raycus RFL-C6000 official, Raycus 6000W sheet table, MAX 6 kW,
Reci 6000W, Gweike); below 1 mm the speed stays the machine's own 1 mm speed with less power. Copper has no recorded
technology - its values are the low end of the charts' overlap (O2, high pressure). The pierce steps (and pierce gas)
come from the machine's nearest recorded thickness of the same Вид (copper: brass, pierce gas oxygen); frequency,
duty, beam and lift height are the machine's usual 5000 Hz / 100 % / 2.1 mm / 30 mm.

A row is skipped when the machine already has any technology for that Вид + thickness, when the template row is
missing, or when the thickness is outside the machine's range. Nothing happens without the machine.

    python -m migration.data_pg3015_proposed_tech
"""
from app import app, db, Machine, MaterialGrade, MachineCutTechnology, _thickness_refusal, fscut

MACHINE_NAME = 'PG3015'
SOURCE = 'Предложение 2026-11-24: интерполация от записаните + таблици 6 kW - за тест'
STEEL, SS, AL, BRASS, GALV, CU = ('Стомана', 'Неръждаема стомана', 'Алуминий', 'Месинг', 'Поцинкована стомана', 'Мед')

# (Вид, mm, gas, nozzle, speed mm/min, bar, peak W, focus mm, height mm, pierce s or None, template: (Вид, mm))
PROPOSALS = [
    (STEEL, 0.5, 'nitrogen', '1.5s/2.0s', 25000, 10, 1500, 0, 0.6, None, (STEEL, 2)),
    (STEEL, 0.8, 'nitrogen', '1.5s/2.0s', 25000, 10, 2000, 0, 0.6, None, (STEEL, 2)),
    (STEEL, 1.2, 'nitrogen', '1.5s/2.0s', 27000, 10, 4000, -0.5, 0.6, None, (STEEL, 2)),
    (STEEL, 1.5, 'nitrogen', '1.5s/2.0s', 30000, 10, 6000, -0.5, 0.6, None, (STEEL, 2)),
    (STEEL, 2.5, 'nitrogen', '1.5s/2.0s', 26000, 15, 6000, -1.2, 0.6, None, (STEEL, 3)),
    (STEEL, 4, 'nitrogen', '2.0s/3.0s', 14000, 13, 6000, -1.8, 0.6, 0.3, (STEEL, 6)),
    (STEEL, 5, 'nitrogen', '2.0s/3.0s', 10500, 12, 6000, -2.2, 0.6, 0.4, (STEEL, 6)),
    (STEEL, 14, 'oxygen', '1.4E', 1200, 1.0, 6000, 12, 0.5, 1.0, (STEEL, 16)),
    (STEEL, 18, 'oxygen', '1.4sp/1.6sp', 750, 1.0, 6000, 12.5, 0.3, 1.3, (STEEL, 20)),
    (STEEL, 22, 'oxygen', '1.6sp', 600, 0.8, 6000, 13.5, 0.3, 2.0, (STEEL, 20)),
    (STEEL, 25, 'oxygen', '1.8S', 450, 0.8, 6000, 14, 0.3, 2.5, (STEEL, 20)),

    (SS, 0.5, 'nitrogen', '1.5s/2.0s', 15000, 12, 1500, 0, 0.5, None, (SS, 2)),
    (SS, 0.8, 'nitrogen', '1.5s/2.0s', 15000, 12, 2000, 0, 0.5, None, (SS, 2)),
    (SS, 1.2, 'nitrogen', '1.5s/2.0s', 19000, 15, 4000, -0.5, 0.5, None, (SS, 2)),
    (SS, 1.5, 'nitrogen', '1.5s/2.0s', 25000, 15, 5000, -0.5, 0.5, None, (SS, 2)),
    (SS, 2.5, 'nitrogen', '2.0s/3.0s', 26000, 15, 6000, -1.2, 0.5, None, (SS, 3)),
    (SS, 5, 'nitrogen', '3.0s/3.5s', 10000, 10, 6000, -2.2, 0.5, 0.4, (SS, 4)),
    (SS, 14, 'nitrogen', '4.0s/5.0s', 1050, 10, 6000, -8.5, 0.5, 0.7, (SS, 16)),

    (AL, 0.5, 'nitrogen', '1.5s/2.0s', 35000, 15, 1500, 0, 0.5, None, (AL, 2)),
    (AL, 0.8, 'nitrogen', '1.5s/2.0s', 35000, 15, 2000, 0, 0.5, None, (AL, 2)),
    (AL, 1.2, 'nitrogen', '1.5s/2.0s', 35000, 15, 4000, -0.5, 0.5, None, (AL, 2)),
    (AL, 1.5, 'nitrogen', '1.5s/2.0s', 35000, 15, 5000, -0.5, 0.5, None, (AL, 2)),
    (AL, 2.5, 'nitrogen', '2.0s/3.0s', 23000, 14, 6000, -1.5, 0.5, None, (AL, 3)),
    (AL, 4, 'nitrogen', '3.0s/3.5s', 11000, 12, 6000, -2.0, 0.5, None, (AL, 6)),
    (AL, 5, 'nitrogen', '3.0s/3.5s', 7500, 12, 6000, -2.2, 0.5, None, (AL, 6)),

    (BRASS, 0.5, 'nitrogen', '1.5s/2.0s', 25000, 15, 1500, 0, 0.5, None, (BRASS, 2)),
    (BRASS, 0.8, 'nitrogen', '1.5s/2.0s', 25000, 15, 2000, 0, 0.5, None, (BRASS, 2)),
    (BRASS, 1.2, 'nitrogen', '1.5s/2.0s', 25000, 15, 4000, -0.5, 0.5, None, (BRASS, 2)),
    (BRASS, 1.5, 'nitrogen', '1.5s/2.0s', 25000, 15, 5000, -0.5, 0.5, None, (BRASS, 2)),
    (BRASS, 2.5, 'nitrogen', '2.0s/3.0s', 14000, 15, 6000, -1.2, 0.5, None, (BRASS, 3)),
    (BRASS, 4, 'nitrogen', '3.0s/3.5s', 7000, 12, 6000, -1.8, 0.5, None, (BRASS, 6)),
    (BRASS, 5, 'nitrogen', '3.0s/3.5s', 5500, 12, 6000, -2.2, 0.5, None, (BRASS, 6)),

    (GALV, 0.5, 'nitrogen', '1.5s/2.0s', 25000, 10, 1500, 0, 0.6, None, (GALV, 2)),
    (GALV, 0.8, 'nitrogen', '1.5s/2.0s', 25000, 10, 2000, 0, 0.6, None, (GALV, 2)),
    (GALV, 1.2, 'nitrogen', '1.5s/2.0s', 27000, 10, 4000, -0.5, 0.6, None, (GALV, 2)),
    (GALV, 1.5, 'nitrogen', '1.5s/2.0s', 30000, 10, 5000, -0.5, 0.6, None, (GALV, 2)),
    (GALV, 2.5, 'nitrogen', '1.5s/2.0s', 26000, 15, 6000, -1.2, 0.6, None, (GALV, 3)),

    # copper: no own data - O2 high pressure, low end of Raycus / MAX / Reci; reflective, start thin
    (CU, 0.5, 'oxygen', '2.0s', 25000, 12, 4000, -0.5, 0.5, None, (BRASS, 2)),
    (CU, 0.8, 'oxygen', '2.0s', 25000, 12, 5000, -0.5, 0.5, None, (BRASS, 2)),
    (CU, 1, 'oxygen', '2.0s', 25000, 12, 6000, -0.5, 0.5, None, (BRASS, 2)),
    (CU, 1.2, 'oxygen', '2.0s', 20000, 12, 6000, -0.8, 0.5, None, (BRASS, 2)),
    (CU, 1.5, 'oxygen', '2.0s', 18000, 12, 6000, -0.8, 0.5, None, (BRASS, 2)),
    (CU, 2, 'oxygen', '2.0s', 13000, 12, 6000, -1.0, 0.5, None, (BRASS, 2)),
    (CU, 2.5, 'oxygen', '2.0s', 10000, 12, 6000, -1.5, 0.5, None, (BRASS, 3)),
    (CU, 3, 'oxygen', '2.0s', 8000, 12, 6000, -2.0, 0.5, None, (BRASS, 3)),
    (CU, 4, 'oxygen', '2.5s', 5500, 12, 6000, -2.5, 0.5, 0.5, (BRASS, 6)),
    (CU, 5, 'oxygen', '3.0s', 3500, 12, 6000, -3.0, 0.5, 0.8, (BRASS, 6)),
    (CU, 6, 'oxygen', '3.0s', 1800, 12, 6000, -3.5, 0.5, 1.0, (BRASS, 6)),
]
PIERCE_COLS = ['pierce_steps'] + [c for c, *_ in fscut.tech_columns() if c[:1] == 'p' and c[1:2].isdigit()]


def _same(rows, grade_id, mm):
    return [t for t in rows if t.grade_id == grade_id and abs(t.thickness_mm - mm) < 0.0005]


def apply():
    """Adds the proposals; returns (added, skipped messages). Flushed, not committed."""
    machine = Machine.query.filter(Machine.name.ilike(f'%{MACHINE_NAME}%'), Machine.is_laser.is_(True)).first()
    if machine is None:
        return 0, [f'няма лазерна машина "{MACHINE_NAME}"']
    grades = {g.name: g.id for g in MaterialGrade.query}
    rows = MachineCutTechnology.query.filter_by(machine_id=machine.id).all()
    added, skipped = 0, []
    for name, mm, gas, nozzle, speed, bar, power, focus, height, pierce, (t_name, t_mm) in PROPOSALS:
        grade_id = grades.get(name)
        if grade_id is None:
            skipped.append(f'{name}: няма такъв Вид')
            continue
        if _same(rows, grade_id, mm):
            continue                                           # the machine already has one - never overwritten
        template = sorted(_same(rows, grades.get(t_name), t_mm), key=lambda t: t.id)
        if not template:
            skipped.append(f'{name} {mm:g} мм: няма шаблон {t_name} {t_mm:g} мм')
            continue
        refusal = _thickness_refusal(machine, grade_id, mm)
        if refusal:
            skipped.append(f'{name} {mm:g} мм: {refusal}')
            continue
        tech = MachineCutTechnology(machine_id=machine.id, grade_id=grade_id, thickness_mm=mm, gas=gas, nozzle=nozzle,
                                    cutting_speed_mm_per_min=speed, pierce_time_sec=pierce, source=SOURCE,
                                    needs_test=True, gas_pressure_bar=bar, peak_power_w=power,
                                    laser_current_pct=round(power / 60, 1), pwm_freq_hz=5000, pwm_ratio_pct=100,
                                    focus_mm=focus, height_mm=height, beam_size_mm=2.1, lift_height_mm=30)
        for col in PIERCE_COLS:
            setattr(tech, col, getattr(template[0], col))
        if name == CU:                                         # copper is cut with oxygen - so is its pierce
            for n in (1, 2, 3):
                if getattr(tech, f'p{n}_gas'):
                    setattr(tech, f'p{n}_gas', 'oxygen')
        db.session.add(tech)
        rows.append(tech)
        added += 1
    db.session.flush()
    return added, skipped


if __name__ == '__main__':
    with app.app_context():
        n, notes = apply()
        db.session.commit()
        print(f'{MACHINE_NAME}: добавени {n} технологии за тест.')
        for line in notes:
            print('  пропусната:', line)
