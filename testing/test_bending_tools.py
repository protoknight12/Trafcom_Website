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

# ---- default tool choice ----
from bending.tools import pick_tools
assert pick_tools([(1, 75), (2, 88)], [(10, 16, 85), (10, 35, 85), (11, 12, 85)], 2, 90) == (2, (11, 12))  # V12 = 6s; punch 88 fits 90 inner
assert pick_tools([(1, 75)], [(10, 16, 85)], 2, 120) == (None, None)    # a 60 deg inner angle: neither 75 nor 85 deg tool fits
assert pick_tools([(1, 75)], [(10, 16, 85)], 2, 80) == (1, (10, 16))
print('ok pick')

# ---- springback and the outside size after the bend ----
from bending.tools import springback_deg, outside_length
assert springback_deg('Неръждаема стомана AISI 304', 16, 2) == 3.0 and springback_deg('S235', 16, 2) == 1.5 and springback_deg('Алуминий', 32, 2) == 5.0
assert abs(outside_length(20, 90, 3, 2) - 22.126) < 0.01      # 20 - BA/2 + OSSB for r=3, t=2, K=0.33
print('ok springback')

# ---- choice of the tool segments by the bend length ----
from bending.tools import pick_segments
SET = {10: 3, 15: 3, 20: 3, 40: 3, 50: 3, 100: 3, 200: 3, 370: 3, 415: 3}
assert pick_segments(100, SET)['segments'] == [100] and pick_segments(100, SET)['excess'] == 0
r = pick_segments(137, SET)
assert r['total'] == 140 and r['excess'] == 3 and sum(r['segments']) == 140        # covers the whole bend with the least overhang
assert pick_segments(3000, SET, 2050) is None                                       # longer than the press
assert pick_segments(100, {40: 1}) is None                                          # not enough pieces in stock
assert sum(pick_segments(2050, SET, 2050)['segments']) == 2050
print('ok segments')

# ---- one row for several bend lengths ----
from bending.tools import plan_row
r = plan_row([40, 40, 130], SET, 2050)
assert not r['missing'] and sum(r['row']) >= 130
w0, w1 = r['windows']
assert w0['need'] == 40 and w0['total'] == 40 and w1['need'] == 130 and w1['total'] >= 130
assert w1['n'] == len(r['row']) or sum(r['row'][w1['start']:w1['start'] + w1['n']]) == w1['total']
assert sum(r['row'][w0['start']:w0['start'] + w0['n']]) == w0['total']              # the short bend's window really is those pieces of the final row
assert plan_row([100], SET)['row'] == [100] and plan_row([3000], SET, 2050)['missing'] == [3000]
assert len(plan_row([50, 50], {50: 1})['windows']) == 1                      # the same length twice needs no second piece
print('ok row')

# ---- nearest sets when nothing covers the bend ----
from bending.tools import nearest_sets
r = nearest_sets(300, {100: 1, 40: 2, 15: 1}, 2050)
assert r['short']['total'] == 195 and r['short']['shortfall'] == 105 and sum(r['short']['segments']) == 195     # the most the stock makes below 300
assert r['buy']['total'] == 300 and sum(r['buy']['add']) == 105 and not r['buy']['excess']                       # + 105 mm of standard pieces covers it
assert nearest_sets(3000, {100: 1}, 2050)['too_long'] and nearest_sets(80, {}, 2050)['short'] is None and sum(nearest_sets(80, {}, 2050)['buy']['add']) == 80
print('ok nearest')

# tools from the model's inner radius: the V that makes it in an air bend (r = 0.16 V), within 5..12 x the thickness
from bending.tools import pick_tools, target_v
assert target_v(2.0) == 12 and target_v(2.0, 3.2) == 20 and target_v(2.0, 0.5) == 10 and target_v(2.0, 9) == 24          # the rule / r/0.16 / the lower and upper limits
dies = [(1, 12, 85), (2, 20, 85), (3, 30, 85)]
assert pick_tools([(1, 85, 0.8)], dies, 2.0, 90)[1][0] == 1 and pick_tools([(1, 85, 0.8)], dies, 2.0, 90, 3.2)[1][0] == 2
assert pick_tools([(1, 85, 0.8), (2, 85, 3.0)], dies, 2.0, 90, 3.2)[0] == 2                                          # same sharpness: the tip radius nearest the model's
print('ok radius tools')
