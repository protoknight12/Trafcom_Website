"""
One-time data step (owner, 2026-11-24), laser PG3015 (6 kW, BOCI BLT421S head):

1. Steel O2 technologies by the machine's OWN CypCut library (6000W切割参数(板机)\\steel): on this head the O2 focus at
   6 kW is +11..+13 for 8-20 mm and +19 at 25 mm (the published 6 kW charts' +3..+6 belong to another head). A steel
   (material code CS) O2 technology of a thickness in LIBRARY whose focus is more than FOCUS_TOLERANCE away from the
   library's gets the library row's cut and pierce fields; its nozzle, Вид and key stay; the old focus / speed go into
   its note. 8 mm is the owner's tested cut (focus +13, 2600 mm/min, nozzle 1.2 double) - stays "tested"; the others
   become "за тест".
2. Nitrogen <-> compressed air copies (the machine blows air through the N2 channel): every N2 technology gets an
   air copy and every air technology an N2 copy, when the machine has none for that Вид + thickness + nozzle - same
   parameters, marked "за тест", not linked to a file in the machine's folder.

Safe to run again (corrected rows match the library; copies exist).

    python -m migration.data_pg3015_o2_focus_and_gas_copies
"""
from app import app, db, Machine, MachineCutTechnology, _grade_code
from migration.data_pg3015_proposed_tech import MACHINE_NAME

FOCUS_TOLERANCE = 1.5
_P = dict(peak_power_w=6000, laser_current_pct=100, pwm_freq_hz=5000, pwm_ratio_pct=100, beam_size_mm=2.1)


def _step(n, gas, bar, freq, duty, focus, height, blow, follow, stay):
    return {f'p{n}_gas': gas, f'p{n}_pressure_bar': bar, f'p{n}_power_w': 6000, f'p{n}_current_pct': 100,
            f'p{n}_freq_hz': freq, f'p{n}_ratio_pct': duty, f'p{n}_focus_mm': focus, f'p{n}_height_mm': height,
            f'p{n}_blow_ms': blow, f'p{n}_follow_ms': follow, f'p{n}_stay_ms': stay}


_P3_AIR_OFF = _step(3, 'air', 5, 5000, 0, 0, 15, 0, 1000, 200)           # an unused third step, as the library has it
_P3_O2_OFF = _step(3, 'oxygen', 5, 5000, 0, 0, 15, 0, 1000, 200)

# thickness -> (library file, fields) - read from the machine's own .fsm files
LIBRARY = {
    8: ('8mm1.2d_D1.fsm', dict(_P, cutting_speed_mm_per_min=2600, pierce_time_sec=2.6, gas_pressure_bar=0.7,
                               focus_mm=13, height_mm=0.6, lift_height_mm=30, pierce_steps=2,
                               **_step(1, 'oxygen', 0.9, 500, 80, -6, 5, 300, 1000, 400),
                               **_step(2, 'oxygen', 0.9, 200, 50, -3, 15, 300, 1000, 200), **_P3_O2_OFF)),
    10: ('10mm1.2d1.5d.fsm', dict(_P, cutting_speed_mm_per_min=2000, pierce_time_sec=3.1, gas_pressure_bar=0.75,
                                  focus_mm=11, height_mm=0.6, lift_height_mm=30, pierce_steps=2,
                                  **_step(1, 'oxygen', 0.9, 500, 80, -6, 5, 300, 1000, 600),
                                  **_step(2, 'oxygen', 0.9, 200, 50, -3, 15, 300, 1000, 500), **_P3_O2_OFF)),
    12: ('12mm1.2d1.5d.fsm', dict(_P, cutting_speed_mm_per_min=1730, pierce_time_sec=3.1, gas_pressure_bar=0.75,
                                  focus_mm=12, height_mm=0.6, lift_height_mm=30, pierce_steps=2,
                                  **_step(1, 'oxygen', 0.75, 500, 80, -6, 5, 300, 1000, 600),
                                  **_step(2, 'oxygen', 0.75, 200, 50, -6, 15, 300, 1000, 500), **_P3_O2_OFF)),
    14: ('14mm1.2sp1.4sp.fsm', dict(_P, cutting_speed_mm_per_min=1400, pierce_time_sec=3.1, gas_pressure_bar=0.65,
                                    focus_mm=11, height_mm=0.6, lift_height_mm=10, pierce_steps=2,
                                    **_step(1, 'oxygen', 0.9, 500, 80, -6, 5, 300, 1000, 600),
                                    **_step(2, 'oxygen', 0.9, 200, 50, -6, 15, 300, 1000, 500), **_P3_AIR_OFF)),
    16: ('16mm1.2sp1.4sp.fsm', dict(_P, cutting_speed_mm_per_min=1200, pierce_time_sec=2.5, gas_pressure_bar=0.7,
                                    focus_mm=12, height_mm=0.3, lift_height_mm=10, pierce_steps=2,
                                    **_step(1, 'oxygen', 1.0, 1000, 80, -4, 10, 500, 1000, 300),
                                    **_step(2, 'oxygen', 1.0, 500, 80, -3, 15, 500, 1000, 200), **_P3_AIR_OFF)),
    20: ('20mm1.4sp1.6sp.fsm', dict(_P, cutting_speed_mm_per_min=700, pierce_time_sec=4.6, gas_pressure_bar=0.8,
                                    focus_mm=13, height_mm=0.3, lift_height_mm=10, pierce_steps=3,
                                    **_step(1, 'oxygen', 1.0, 300, 80, -4, 10, 600, 1000, 800),
                                    **_step(2, 'oxygen', 1.1, 1500, 60, -4, 14, 600, 1000, 500),
                                    **_step(3, 'oxygen', 1.3, 3000, 80, -2, 20, 300, 1000, 300))),
    25: ('25mm1.4sp1.6sp.fsm', dict(_P, cutting_speed_mm_per_min=300, pierce_time_sec=7.3, gas_pressure_bar=1.2,
                                    focus_mm=19, height_mm=0.3, lift_height_mm=30, pierce_steps=3,
                                    **_step(1, 'oxygen', 1.2, 800, 70, -7, 7, 1000, 2000, 2000),
                                    **_step(2, 'oxygen', 1.2, 1500, 70, -5, 15, 500, 800, 1500),
                                    **_step(3, 'oxygen', 1.2, 20, 50, -3, 19, 200, 800, 200))),
}
TESTED = {8}                         # the owner's own test cut confirmed these
COPY = {'nitrogen': 'air', 'air': 'nitrogen'}
_SKIP = {'id', 'gas', 'folder_file', 'folder_file_hash', 'folder_app_hash', 'source', 'needs_test', 'note'}


def _note(old, add):
    return (old + '\n' if old else '') + add


def apply():
    """Returns (corrected, copied, notes). Flushed, not committed."""
    machine = Machine.query.filter(Machine.name.ilike(f'%{MACHINE_NAME}%'), Machine.is_laser.is_(True)).first()
    if machine is None:
        return 0, 0, [f'няма лазерна машина "{MACHINE_NAME}"']
    techs = MachineCutTechnology.query.filter_by(machine_id=machine.id).all()
    corrected, notes = 0, []
    for t in techs:
        if t.gas != 'oxygen' or _grade_code(t.grade.name if t.grade else '') != 'CS':
            continue
        lib = next((v for mm, v in LIBRARY.items() if abs(t.thickness_mm - mm) < 0.0005), None)
        if lib is None or (t.focus_mm is not None and abs(t.focus_mm - lib[1]['focus_mm']) <= FOCUS_TOLERANCE):
            continue
        name, fields = lib
        before = f'фокус {t.focus_mm if t.focus_mm is not None else "-"}, {t.cutting_speed_mm_per_min or "-"} мм/мин'
        for col, v in fields.items():
            setattr(t, col, v)
        t.needs_test = round(t.thickness_mm) not in TESTED
        t.note = _note(t.note, f'2026-11-24: по библиотеката на машината ({name}); преди: {before}')
        corrected += 1
        notes.append(f'коригирана: {t.thickness_mm:g} мм O2 ({before} -> фокус {fields["focus_mm"]:g})')

    copied = 0
    cols = [c.key for c in MachineCutTechnology.__table__.columns if c.key not in _SKIP]
    have = {(t.grade_id, round(t.thickness_mm, 3), t.gas, t.nozzle) for t in techs}
    for t in list(techs):
        to = COPY.get(t.gas)
        if not to or (t.grade_id, round(t.thickness_mm, 3), to, t.nozzle) in have:
            continue
        c = MachineCutTechnology(**{k: getattr(t, k) for k in cols})
        c.gas = to
        for n in (1, 2, 3):                       # pierce steps on the cutting gas follow it
            if getattr(c, f'p{n}_gas') == t.gas:
                setattr(c, f'p{n}_gas', to)
        c.needs_test = True
        c.source = f'Копие от {"азот" if t.gas == "nitrogen" else "сгъстен въздух"} 2026-11-24 - за тест'
        c.note = t.note
        db.session.add(c)
        have.add((t.grade_id, round(t.thickness_mm, 3), to, t.nozzle))
        copied += 1
    db.session.flush()
    return corrected, copied, notes


if __name__ == '__main__':
    with app.app_context():
        n_fix, n_copy, lines = apply()
        db.session.commit()
        print(f'{MACHINE_NAME}: коригирани {n_fix} O2 технологии по библиотеката, копия N2/въздух: {n_copy}.')
        for line in lines:
            print('  ', line)
