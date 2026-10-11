"""bending.tools: hand-computed force / die choice. Run: python -m testing.test_bending_tools"""
from bending.tools import force_kn_per_m, rank_dies, recommended_v


def close(a, b, tol=1e-6):
    assert abs(a - b) <= tol, (a, b)


close(force_kn_per_m(400, 2, 16), 142.0)   # 1.42*400*4/16
close(force_kn_per_m(400, 3, 24), 1.42 * 400 * 9 / 24)
assert (recommended_v(2), recommended_v(5), recommended_v(12)) == (12, 40, 120)
dies = [('V8', 8, None), ('V12', 12, 100.0), ('V16', 16, 300.0)]
r = rank_dies(dies, 400, 2, 2000, machine_kn=1000)  # 2 mm: recommended V12
assert [d['code'] for d in r] == ['V16', 'V12', 'V8']  # valid first, then closest to the recommended V12
by = {d['code']: d for d in r}
assert by['V12']['ok'] is False and 'инструмента' in by['V12']['notes'][0]   # 189 kN/m > 100
assert by['V8']['ok'] is False and any('малък' in n for n in by['V8']['notes'])
assert by['V16']['ok'] and by['V16']['total_kn'] == 284.0
assert rank_dies([('V16', 16, None)], 400, 2, 20000, machine_kn=1000)[0]['notes']  # 20 m is above the machine
for bad in ((0, 2, 16), (400, 0, 16), (400, 2, 0)):
    try:
        force_kn_per_m(*bad)
        raise SystemExit('expected ValueError')
    except ValueError:
        pass
print('ok')

# ---- air-bend depth / step checks ----
from bending.tools import air_bend_depth, step_checks
d, tr = air_bend_depth(16, 2, 90)   # 90 deg: depth = V/2 - t*sqrt(2) = 8 - 2.83
assert abs(d - 5.17) < 0.01 and abs(tr - 7.17) < 0.01
assert step_checks(90, 2, 16, punch_angle=75, die_angle=85) == []
assert 'поансонът' in step_checks(90, 2, 16, punch_angle=95)[0]
assert 'Нужен ход' in step_checks(90, 2, 16, stroke_mm=5)[0]
assert 'отварянето' in step_checks(90, 2, 16, punch_h=97, die_h=60, daylight_mm=150)[0]
print('ok air bend')
