"""
One-off seed: the shop's own tool set delivered with the press brakes (compatible with both): punch P.97.75.r08 and the four-way die M460.r
(ONE 60 x 60 mm block with a V groove on each side: V50 top / V16 right / V35 bottom / V22 left, all 85 deg, shoulder radii R3 on the V50
side and R2 on the others, max 800 kN/m; the valley radii are estimated from the drawing),
3 sets (punch segment lengths = the dies'; end punches are added later), each set with segments 10, 15, 20, 40, 50, 100, 200, 370 and 415 mm. Tools go straight into the library; every
segment gets its own ERP number. Idempotent: only missing segments are added (re-run after adding a length to LENGTHS).

    python -m migration.seed_bend_tools_m460r
"""
import json
import math

from app import BendStep, BendTool, BendToolSegment, _next_erp_number, app, db
from bending.profile import multi_v_die, tip_offset
from bending.tools import G

# M460.r, clockwise from the top side as in the drawing: (V opening, angle, shoulder radius, valley radius). The valley radii are NOT on the
# drawing - they were estimated from it (+-1 mm) and do not affect a bend, the sheet never reaches the valley.
M460R_SIDES = [(50, 85, 3, 5), (16, 85, 2, 1), (35, 85, 2, 3), (22, 85, 2, 3)]
DIE_SIZE = 60  # a 60 x 60 block: its 60 mm width is the press brakes' die channel
LENGTHS = [10, 15, 20, 40, 50, 100, 200, 370, 415]
SETS = 3


# Outline of P.97.75.r08 (working part only) read off the manufacturer's drawing: vertices [x, y, fillet r, arc a], mm, y up, measured
# from the theoretical sharp apex. Real numbers: 75 deg tip (two 37.5 deg flanks), tip fillet R0.8, concave relief arc R24.6 (a > 0 =
# bulges into the material), shoulder at 67.6. The rest is read off the picture to about 0.2 mm. seed() moves the origin to the lowest
# point of the rounded tip. The clamping tang is standard for the press brake's beam and is not stored (bending.profile.CLAMP).
_T = math.tan(math.radians(37.5))
P97_PROFILE = [[0, 0, 0.8, 0], [6.2, 6.2 / _T, 0, 24.6], [6.2, 43.23, 0, 0], [6.2, 64.33, 0, 0], [5.34, 67.6, 0, 0],
               [-19.63, 67.6, 0, 0], [-19.63, 27.99, 0, 0], [-6.2, 6.2 / _T, 0, 0]]

# (code, kind, height, angle, radius, V, max kN/m); punch height = working height 67.6 (97.6 overall incl. the beam clamp)
TOOLS = [('P.97.75.r08', 'punch', 67.6, 75, 0.8, None, 100 * G)] +         [('M460.r', 'die', DIE_SIZE, 85, None, None, 800)]


def _merge_legacy():
    """An earlier version made four dies 'M460.r V16/V22/V35/V50'. They are one die: keep the V16 row (renamed M460.r, with its segments) and
    drop the other three - but only when nothing was done to them (all segments in stock, never used, no place, no program step)."""
    old = {t.code: t for t in BendTool.query.filter(BendTool.code.like('M460.r V%')).all()}
    if not old or BendTool.query.filter_by(code='M460.r').first():
        return 0
    extra = [t for c, t in old.items() if c != 'M460.r V16']
    for t in extra:
        used = any(sg.status != 'in_stock' or sg.bend_count or sg.location_id for sg in t.segments) or BendStep.query.filter(
            (BendStep.die_id == t.id) | (BendStep.punch_id == t.id)).count()
        if used:
            raise SystemExit(f'{t.code} already has history (segment status / bends / place / program step) - merge M460.r by hand.')
    keep = old.get('M460.r V16') or next(iter(old.values()))
    for t in extra:
        for sg in list(t.segments):
            db.session.delete(sg)
        db.session.delete(t)
    keep.code, keep.v_opening_mm, keep.radius_mm, keep.height_mm, keep.profile_json = 'M460.r', None, None, DIE_SIZE, None
    db.session.flush()
    return len(extra)


def seed():
    """Returns (tools created, segments created); tops every tool up to SETS segments per length."""
    tools = segs = 0
    _merge_legacy()
    for code, kind, h, angle, r, v, kn in TOOLS:
        tool = BendTool.query.filter_by(code=code).first()
        if not tool:
            tool = BendTool(code=code, kind=kind, height_mm=h, angle_deg=angle, radius_mm=r, v_opening_mm=v, max_kn_per_m=kn, status='in_library')
            db.session.add(tool)
            db.session.flush()
            tools += 1
        if code == 'P.97.75.r08' and not tool.profile_json:
            off = tip_offset(angle, r)
            tool.profile_json = json.dumps([[round(x, 2), round(y - off, 2), rr, aa] for x, y, rr, aa in P97_PROFILE])
        if kind == 'die' and not tool.profile_json:
            tool.profile_json = json.dumps(multi_v_die(DIE_SIZE, M460R_SIDES))
        have = [s.length_mm for s in tool.segments]
        for length in LENGTHS:
            for _ in range(SETS - have.count(length)):
                db.session.add(BendToolSegment(tool=tool, length_mm=length, erp_number=_next_erp_number()))
                db.session.flush()  # the next ERP number counts this one
                segs += 1
    db.session.commit()
    return tools, segs


if __name__ == '__main__':
    with app.app_context():
        print('Инструменти: %d, сегменти: %d' % seed())
