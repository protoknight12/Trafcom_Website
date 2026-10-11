"""bending.profile: outline with real radii (fillets, arcs), checks, DXF import, standard clamp, pictures.
Hand-computed expectations. Run: python -m testing.test_bending_profile"""
import io
import math
import xml.etree.ElementTree as ET

import ezdxf

from bending.profile import (CLAMP, DIE_CHANNEL, clamp_polygon, clip_to_seat, default_profile, derived, normalize, outline, polygon_from_dxf,
                             polygon_svg, radii_report, tip_offset, tool_svg, validate_polygon)


def close(a, b, tol=1e-2):
    assert abs(a - b) <= tol, (a, b)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return
    raise SystemExit(f'expected ValueError from {fn.__name__}{a}')


# ---- fillet: a right-angle corner with R2 loses a 2 mm tangent length on each side, arc centre at (2, 2) ----
sq = [[0, 0, 2, 0], [20, 0, 0, 0], [20, 20, 0, 0], [0, 20, 0, 0]]
d = outline(sq)
assert [0.0, 2.0] in d and [2.0, 0.0] in d  # tangent points (0, 2) and (2, 0)
arc_pts = [p for p in d if p[0] < 2.0001 and p[1] < 2.0001]
assert len(arc_pts) > 10 and all(abs(math.hypot(p[0] - 2, p[1] - 2) - 2) < 2e-3 for p in arc_pts)  # all on the R2 circle round (2, 2)
assert radii_report(sq) == [{'kind': 'fillet', 'index': 1, 'radius': 2.0, 'x': 0.0, 'y': 0.0}]
# area of the rounded square = 400 - (1 - pi/4) * R^2 = 400 - 0.8584
close(derived(sq, 'die')['area'], 400 - (1 - math.pi / 4) * 4, 0.05)

# ---- arc: the top edge (0,0)->(10,0) as an R10 arc; its sagitta is 10 - sqrt(100 - 25) = 1.34 ----
up = [[0, 0, 0, 10], [10, 0, 0, 0], [10, -10, 0, 0], [0, -10, 0, 0]]   # typed clockwise: left of rightward travel is up, so a > 0 bulges up
close(max(p[1] for p in outline(up)), 10 - math.sqrt(75), 0.03)           # the top edge bulges UP (out of the material)
down = [[0, 0, 0, -10], [10, 0, 0, 0], [10, -10, 0, 0], [0, -10, 0, 0]]  # the sign flips the side (into the material)
close(max(p[1] for p in outline(down)), 0.0, 0.01)
close(min(p[1] for p in outline(down) if 1 < p[0] < 9), -(10 - math.sqrt(75)), 0.03)
raises(normalize, [[0, 0, 0, 3], [10, 0, 0, 0], [10, 10, 0, 0]])  # R3 cannot span a 10 mm chord
raises(normalize, [[0, 0, 5, 0], [4, 0, 0, 0], [4, 10, 0, 0], [0, 10, 0, 0]])  # fillet too large for the 4 mm edge

# ---- a punch: 75 deg tip, R0.8 at the apex, 24.6 concave relief; angle and radius are read back from the outline ----
t = math.tan(math.radians(37.5))
punch = [[0, 0, 0.8, 0], [6.2, 6.2 / t, 0, 24.6], [6.2, 43.23, 0, 0], [6.2, 64, 0, 0], [-19.63, 64, 0, 0], [-19.63, 27.99, 0, 0], [-6.2, 6.2 / t, 0, 0]]
dv = derived(punch, 'punch')
close(dv['tip_angle'], 75.0); assert dv['tip_radius'] == 0.8
close(min(p[1] for p in outline(punch)), tip_offset(75, 0.8), 0.01)  # lowest point sits 0.514 above the theoretical apex
close(tip_offset(75, 0.8), 0.8 / math.sin(math.radians(37.5)) - 0.8, 1e-9)
rep = radii_report(punch)
assert [(r['kind'], r['radius'], r.get('concave')) for r in rep] == [('fillet', 0.8, None), ('arc', 24.6, True)]
# the relief reaches inside the straight chord x = 6.2 by the sagitta of an R24.6 arc over a 35.15 mm chord
chord = 43.23 - 6.2 / t
sag = 24.6 - math.sqrt(24.6 ** 2 - (chord / 2) ** 2)
close(6.2 - min(p[0] for p in outline(punch) if 8.5 < p[1] < 43 and p[0] > -5), sag, 0.1)
validate_polygon(punch)

# ---- the same outline typed clockwise or mirrored is the same shape ----
mirrored = [[-x, y, r, -a] for x, y, r, a in punch]
close(derived(mirrored, 'punch')['tip_angle'], 75.0)
assert [(r['kind'], r['concave']) for r in radii_report(mirrored) if r['kind'] == 'arc'] == [('arc', True)]
close(derived(mirrored, 'punch')['area'], dv['area'], 0.05)

# ---- die: 60 x 60 body, V 16 mm, 85 deg, shoulders R2 (the radius given for the die), sharp valley ----
die = default_profile('die', 85, 2, 60, 16)
x = derived(die, 'die')
assert x['width'] == 60 and x['base_width'] == DIE_CHANNEL == 60.0 and x['height'] == 60   # the base fits the 60 mm die channel
close(x['valley_angle'], 85.0); assert x['v_opening'] == 16.0 and x['shoulder_radii'] == [2.0, 2.0] and x['valley_radius'] is None
close(x['groove_depth'], 8 / math.tan(math.radians(42.5)))
assert [(r['kind'], r['radius']) for r in radii_report(die)] == [('fillet', 2.0), ('fillet', 2.0)]
die[2][2] = 1.5  # a radius at the bottom of the V is its own, separate number
assert derived(die, 'die')['valley_radius'] == 1.5
# a die may be narrower above its base: only the bottom `base_height` mm must be the 60 mm of the die channel
narrow = [[-20, 0, 0, 0], [-8, 0, 2, 0], [0, -8.7, 0, 0], [8, 0, 2, 0], [20, 0, 0, 0], [20, -40, 0, 0], [30, -40, 0, 0], [30, -60, 0, 0], [-30, -60, 0, 0], [-30, -40, 0, 0], [-20, -40, 0, 0]]
validate_polygon(narrow)
bw = derived(narrow, 'die', 20)
assert bw['base_width'] == 60 and bw['base_width_max'] == 60           # the bottom 20 mm are full width
bw = derived(narrow, 'die', 30)
assert bw['base_width'] == 40 and bw['base_width_max'] == 60           # a 30 mm base would include the narrower part
assert derived(narrow, 'die')['base_width'] == 60                       # no base height given: only the bottom edge is checked

# ---- a four-way die (M460.r): ONE block with a V groove on each side ----
from bending.profile import grooves, multi_v_die, rotated

m460 = multi_v_die(60, [(50, 85, 3, 5), (16, 85, 2, 1), (35, 85, 2, 3), (22, 85, 2, 3)])   # clockwise from the top side
validate_polygon(m460)
gs = grooves(m460)
assert [g['v'] for g in gs] == [16, 22, 35, 50]
for g in gs:
    close(g['angle'], 85.0, 0.1)
    close(g['depth'], (g['v'] / 2) / math.tan(math.radians(42.5)), 0.05)           # sharp-apex depth of an 85 deg V
assert {g['v']: g['shoulder_radii'] for g in gs} == {16: [2.0, 2.0], 22: [2.0, 2.0], 35: [2.0, 2.0], 50: [3.0, 3.0]}
assert {g['v']: g['valley_radius'] for g in gs} == {16: 1.0, 22: 3.0, 35: 3.0, 50: 5.0}
rot = {g['v']: g['rotation'] for g in gs}
assert rot[50] == 0 and rot[16] == 90 and abs(rot[35]) == 180 and rot[22] == -90          # top / right / bottom / left side
dm = derived(m460, 'die')
assert dm['width'] == 60 and dm['height'] == 60 and dm['base_width'] == 60 and len(dm['grooves']) == 4 and dm['v_opening'] == 50  # the V that faces up as drawn
# standing on the machine with the V16 up: turn it so that groove opens upward; it then reads as a single-V die with the same numbers
up = derived(rotated(m460, rot[16]), 'die')
assert up['v_opening'] == 16 and up['shoulder_radii'] == [2.0, 2.0]
assert polygon_svg(m460, 'die').count('<path') == 1

# ---- checks ----
raises(validate_polygon, [[0, 0], [1, 1]])
raises(validate_polygon, [[0, 0], [10, 0], [20, 0]])                                    # flat
raises(validate_polygon, [[0, 0], [10, 0], [10, 10], [5, -5], [0, 10]])                  # crosses itself
raises(validate_polygon, [[0, 0], [1e6, 0], [0, 5]])                                     # outside +-1000 mm
raises(validate_polygon, [['a', 1], [2, 3], [4, 5]])
raises(validate_polygon, [[0, 0, -1, 0], [10, 0], [10, 10]])                             # negative radius
raises(validate_polygon, [[0, 0, 1, 5], [10, 0], [10, 10], [0, 10]])                     # a fillet next to an arc
assert validate_polygon([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]) == [[0, 0, 0, 0], [10, 0, 0, 0], [10, 10, 0, 0], [0, 10, 0, 0]]  # legacy 2-value points, closing point dropped
for kind in ('punch', 'die'):
    validate_polygon(default_profile(kind, 75, 0.8, 67.6, 16))

# ---- DXF: arcs stay arcs with their radius; tip goes to the origin ----
doc = ezdxf.new()
m = doc.modelspace()
m.add_line((100, 50), (110, 50))
m.add_line((110, 50), (110, 70))
m.add_arc((100, 70), 10, 0, 90)          # (110,70) -> (100,80), counter-clockwise quarter of R10
m.add_line((100, 80), (90, 80))
m.add_line((90, 80), (90, 50))
m.add_line((90, 50), (100, 50))
back = polygon_from_dxf(doc, 'punch')
assert min(p[1] for p in outline(back)) == 0 and abs(sum(p[0] for p in outline(back) if p[1] == 0) / max(1, sum(1 for p in outline(back) if p[1] == 0))) < 1e-6
arcs = [r for r in radii_report(back) if r['kind'] == 'arc']
assert len(arcs) == 1 and arcs[0]['radius'] == 10.0 and arcs[0]['concave'] is False  # a convex corner arc, R preserved
doc = ezdxf.new()
doc.modelspace().add_lwpolyline([(100, 50, 0, 0, 0), (110, 70, 0, 0, 0.3), (90, 70, 0, 0, 0)], format='xyseb', close=True)
assert polygon_from_dxf(doc, 'punch')
doc = ezdxf.new()
doc.modelspace().add_line((0, 0), (5, 5))
raises(polygon_from_dxf, doc, 'punch')   # open outline
doc = ezdxf.new()
doc.modelspace().add_arc((0, 0), 10, 0, 270)
raises(polygon_from_dxf, doc, 'punch')   # arc over 180 degrees

# ---- standard clamp: drawn, never stored; a full catalogue outline is cut back at the seat line ----
work = [[-19.63, 0], [6.2, 0], [6.2, 67], [-19.63, 67]]
cp = clamp_polygon(work)
close(min(p[0] for p in cp), -19.63); close(max(p[0] for p in cp), -19.63 + CLAMP['width'])
close(max(p[1] for p in cp), 67 + CLAMP['height']); validate_polygon(cp)
full = [[-19.63, 0], [6.2, 0], [6.2, 67], [-6.72, 67], [-6.72, 97], [-19.63, 97]]
cut = clip_to_seat(full, 67)
assert max(p[1] for p in cut) == 67 and len(validate_polygon(cut)) >= 4
assert [p[:2] for p in clip_to_seat(work, 67)] == work  # nothing above the seat -> unchanged

# ---- pictures: punches get raster + clamp + outline, dies only the outline; a tool without a profile still gets a picture ----
svg = polygon_svg(punch, 'punch', 'P')
ET.fromstring(svg)
assert svg.count('<path') == 3 and '5 mm' in svg and '<title>P</title>' in svg
assert polygon_svg(die, 'die').count('<path') == 1
assert tool_svg('punch', 75, 0.8, 67.6, None).count('<path') == 3 and tool_svg('die', None, None, None, None).startswith('<svg')
print('ok')

# ---- collision of the whole part with the punch in the cross-section (real P.97.75.r08 + M460.r) ----
import migration.seed_bend_tools_m460r as _m
from bending.profile import step_collision, step_svg
_off = tip_offset(75, 0.8)
_P = [[x, y - _off, r, a] for x, y, r, a in _m.P97_PROFILE]
_D = multi_v_die(_m.DIE_SIZE, _m.M460R_SIDES)
assert step_collision(_P, _D, 16, 2, 90) == []                                   # a single bend: free
assert step_collision(_P, _D, 16, 2, 90, left=[(40, 90), (80, None)]) != []      # a tall flange curling back towards the punch hits it
assert 'КОЛИЗИЯ' in step_svg(_P, _D, 16, 2, 90, left=[(40, 90), (80, None)]) and 'КОЛИЗИЯ' not in step_svg(_P, _D, 16, 2, 90)
assert step_svg(_P, _D, 16, 2, 90, right=[(30, None)]).count('stroke-dasharray="2.5 1.5"') == 1   # the part before the bend is dashed
# every pair is checked: gauge finger against the punch (a very short flange), the part against the die (a long flange folding down), and so on
assert step_collision(_P, _D, 16, 2, 90, right=[(5, None)]) == []   # a very short flange: the finger would meet the punch - it is retracted instead (see step_trajectory)
assert 'детайл - матрица' in step_collision(_P, _D, 16, 2, 90, left=[(15, -90), (60, None)])
assert 'детайл - поансон' in step_collision(_P, _D, 16, 2, 90, left=[(40, 90), (80, None)])
print('ok pairs')
# exact support: the sheet's bottom line is tangent to the die's shoulder circles; springback makes the tool go deeper
from bending.profile import _step_geom
g0 = _step_geom(_P, _D, 16, 2, 90, [], [])
(cx, cy), = [g0['contacts'][1]]
assert abs(cy - (-2 + 2 * math.sin(math.radians(45)))) < 1e-6 and 5 < cx < 10        # R2 shoulder, 90 deg sheet: contact point right next to V/2 = 8
g1 = _step_geom(_P, _D, 16, 2, 90, [], [], overbend=3.0)
assert min(y for _, y in g1['punch']) < min(y for _, y in g0['punch']) - 0.15          # 93 deg bent -> the punch goes deeper than for 90
print('ok exact support')
# the part as it lies on the die BEFORE the stroke is checked too: a flange hanging down right at the die's edge goes through the die
assert 'детайл преди огъването - матрица' in step_collision(_P, _D, 16, 2, 90, left=[(25, -90), (100, None)])
assert 'детайл преди огъването - матрица' not in step_collision(_P, _D, 16, 2, 90, left=[(60, -90), (100, None)])   # hangs free in front of the die
print('ok before')
# the whole bend is followed: a back flange curling up at the end of the part swings into the finger half way -> the finger has to be retracted
from bending.profile import step_trajectory
tr = step_trajectory(_P, _D, 16, 2, 90, left=[(60, None)], right=[(20, 90), (15, None)])
assert tr['hits'] == [] and tr['retract_deg'] is not None and 6 <= tr['retract_deg'] < 90
assert step_trajectory(_P, _D, 16, 2, 90, right=[(40, None)])['retract_deg'] is None     # a plain flat part leaves the finger by itself
print('ok trajectory')
# a retracted finger is drawn again, dashed, where it stands after the retract
assert 'отдръпнат' in step_svg(_P, _D, 16, 2, 90, left=[(60, None)], right=[(20, 90), (15, None)])
assert 'отдръпнат' not in step_svg(_P, _D, 16, 2, 90, right=[(40, None)])
print('ok retract drawing')
