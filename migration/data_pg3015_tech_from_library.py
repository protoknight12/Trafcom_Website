"""
One-time data step (owner, 2026-11-24): corrects four of PG3015's proposed O2 technologies for steel
(migration/data_pg3015_proposed_tech.py, still "за тест") by the machine's OWN CypCut library (its .fsm files, 6000W
BLT4XX head) - the proposals had been made from published 6 kW charts, whose focus values belong to another head.

  14 mm  = the library's 14mm1.2sp1.4sp.fsm (every cut and pierce field, nozzle 1.2sp/1.4sp)
  25 mm  = the library's 25mm1.4sp1.6sp.fsm (focus +19, 1.2 bar, 300 mm/min, three pierce steps, nozzle 1.4sp/1.6sp)
  18 mm  = between the library's 16 mm (1200 mm/min, +12, 0.7 bar) and 20 mm (700, +13, 0.8): 900, +12.5, 0.75
  22 mm  = between the library's 20 mm and 25 mm (300, +19, 1.2): 500, +15, 0.95; nozzle 1.4sp/1.6sp
  18 / 22 keep their pierce steps (from the machine's 20 mm technology).

Only a row still exactly as the proposal step left it is changed (source, "за тест", speed and focus unchanged); one
the owner edited, tested or replaced is left alone, and so is a thickness that already has a technology with the new
nozzle. Every changed row stays "за тест". Safe to run again (a changed row no longer matches).

    python -m migration.data_pg3015_tech_from_library
"""
from app import app, db, Machine, MaterialGrade, MachineCutTechnology
from migration.data_pg3015_proposed_tech import MACHINE_NAME, SOURCE, STEEL

NEW_SOURCE = 'Предложение 2026-11-24, по библиотеката на машината (CypCut .fsm) - за тест'
# 5000 Hz / 100 % / 2.1 mm beam / 6000 W at 100 % are the library's on every O2 row
_CUT = {'peak_power_w': 6000, 'laser_current_pct': 100, 'pwm_freq_hz': 5000, 'pwm_ratio_pct': 100, 'beam_size_mm': 2.1}


def _steps(*steps):
    """(gas, bar, freq Hz, duty %, focus, height, blow ms, follow ms, stay ms) per pierce step -> p<N>_* fields."""
    out = {'pierce_steps': len(steps)}
    for n, (gas, bar, freq, duty, focus, height, blow, follow, stay) in enumerate(steps, 1):
        out.update({f'p{n}_gas': gas, f'p{n}_pressure_bar': bar, f'p{n}_power_w': 6000, f'p{n}_current_pct': 100,
                    f'p{n}_freq_hz': freq, f'p{n}_ratio_pct': duty, f'p{n}_focus_mm': focus,
                    f'p{n}_height_mm': height, f'p{n}_blow_ms': blow, f'p{n}_follow_ms': follow,
                    f'p{n}_stay_ms': stay})
    return out


# thickness -> ((proposal's speed, focus) = still untouched, new values)
FIXES = {
    14: ((1200, 12), dict(_CUT, nozzle='1.2sp/1.4sp', cutting_speed_mm_per_min=1400, pierce_time_sec=3.1,
                          gas_pressure_bar=0.65, focus_mm=11, height_mm=0.6, lift_height_mm=10,
                          source='Библиотека PG3015: 14mm1.2sp1.4sp.fsm - за тест',
                          **_steps(('oxygen', 0.9, 500, 80, -6, 5, 300, 1000, 600),
                                   ('oxygen', 0.9, 200, 50, -6, 15, 300, 1000, 500)),
                          p3_gas='air', p3_pressure_bar=5, p3_power_w=6000, p3_current_pct=100, p3_freq_hz=5000,
                          p3_ratio_pct=0, p3_focus_mm=0, p3_height_mm=15, p3_blow_ms=0, p3_follow_ms=1000,
                          p3_stay_ms=200)),
    18: ((750, 12.5), dict(cutting_speed_mm_per_min=900, gas_pressure_bar=0.75, focus_mm=12.5, height_mm=0.3,
                           source=NEW_SOURCE)),
    22: ((600, 13.5), dict(nozzle='1.4sp/1.6sp', cutting_speed_mm_per_min=500, gas_pressure_bar=0.95, focus_mm=15,
                           height_mm=0.3, source=NEW_SOURCE)),
    25: ((450, 14), dict(_CUT, nozzle='1.4sp/1.6sp', cutting_speed_mm_per_min=300, pierce_time_sec=7.3,
                         gas_pressure_bar=1.2, focus_mm=19, height_mm=0.3, lift_height_mm=30,
                         source='Библиотека PG3015: 25mm1.4sp1.6sp.fsm - за тест',
                         **_steps(('oxygen', 1.2, 800, 70, -7, 7, 1000, 2000, 2000),
                                  ('oxygen', 1.2, 1500, 70, -5, 15, 500, 800, 1500),
                                  ('oxygen', 1.2, 20, 50, -3, 19, 200, 800, 200)))),
}


def apply():
    """Changes the untouched proposals; returns (changed, notes). Flushed, not committed."""
    machine = Machine.query.filter(Machine.name.ilike(f'%{MACHINE_NAME}%'), Machine.is_laser.is_(True)).first()
    grade = MaterialGrade.query.filter_by(name=STEEL).first()
    if machine is None or grade is None:
        return 0, [f'няма лазерна машина "{MACHINE_NAME}" или Вид "{STEEL}"']
    rows = MachineCutTechnology.query.filter_by(machine_id=machine.id, grade_id=grade.id, gas='oxygen').all()
    changed, notes = 0, []
    for mm, ((speed, focus), new) in FIXES.items():
        same = [t for t in rows if abs(t.thickness_mm - mm) < 0.0005]
        prop = next((t for t in same if t.source == SOURCE and t.needs_test and t.cutting_speed_mm_per_min == speed
                     and t.focus_mm == focus), None)
        if prop is None:
            notes.append(f'{mm} мм: няма непроменено предложение')
            continue
        nozzle = new.get('nozzle', prop.nozzle)
        if any(t is not prop and t.nozzle == nozzle for t in same):
            notes.append(f'{mm} мм: вече има технология с дюза {nozzle}')
            continue
        for col, v in new.items():
            setattr(prop, col, v)
        changed += 1
    db.session.flush()
    return changed, notes


if __name__ == '__main__':
    with app.app_context():
        n, lines = apply()
        db.session.commit()
        print(f'{MACHINE_NAME}: коригирани {n} предложения по библиотеката.')
        for line in lines:
            print('  пропуснато:', line)
