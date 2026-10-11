"""
One-off seed: the shop's own tool set delivered with the press brakes (compatible with both): punch P.97.75.r08 and the die set M460.r (4 dies, V 16/22/35/50 mm, 85 deg, die radius 2/2/2/3 mm, max 800 kN/m),
3 sets (punch segment lengths = the dies'; end punches are added later), each set with segments 10, 15, 20, 40, 50, 100, 200, 370 and 415 mm. Tools go straight into the library; every
segment gets its own ERP number. Idempotent: only missing segments are added (re-run after adding a length to LENGTHS).

    python -m migration.seed_bend_tools_m460r
"""
import json
import math

from app import BendTool, BendToolSegment, _next_erp_number, app, db
from bending.profile import tip_offset
from bending.tools import G

DIES = [(16, 2), (22, 2), (35, 2), (50, 3)]  # (V opening, die radius)
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
TOOLS = [('P.97.75.r08', 'punch', 67.6, 75, 0.8, None, 100 * G)] +         [(f'M460.r V{v}', 'die', None, 85, r, v, 800) for v, r in DIES]


def seed():
    """Returns (tools created, segments created); tops every tool up to SETS segments per length."""
    tools = segs = 0
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
