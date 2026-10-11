"""
One-off seed: the shop's own tool set delivered with the press brakes (compatible with both): punch P.97.75.r08 and the die set M460.r (4 dies, V 16/22/35/50 mm, 85 deg, die radius 2/2/2/3 mm, max 800 kN/m),
3 sets (punch segment lengths = the dies'; end punches are added later), each set with segments 10, 15, 20, 40, 50, 100, 200 and 370 mm. Tools go straight into the library; every
segment gets its own ERP number. Idempotent: a die that already exists is left alone.

    python -m migration.seed_bend_tools_m460r
"""
from app import BendTool, BendToolSegment, _next_erp_number, app, db
from bending.tools import G

DIES = [(16, 2), (22, 2), (35, 2), (50, 3)]  # (V opening, die radius)
LENGTHS = [10, 15, 20, 40, 50, 100, 200, 370]
SETS = 3


# (code, kind, height, angle, radius, V, max kN/m); punch height = working height 67.6 (97.6 overall incl. the beam clamp)
TOOLS = [('P.97.75.r08', 'punch', 67.6, 75, 0.8, None, 100 * G)] +         [(f'M460.r V{v}', 'die', None, 85, r, v, 800) for v, r in DIES]


def seed():
    """Returns (tools created, segments created)."""
    dies = segs = 0
    for code, kind, h, angle, r, v, kn in TOOLS:
        if BendTool.query.filter_by(code=code).first():
            continue
        tool = BendTool(code=code, kind=kind, height_mm=h, angle_deg=angle, radius_mm=r, v_opening_mm=v, max_kn_per_m=kn, status='in_library')
        db.session.add(tool)
        dies += 1
        for _ in range(SETS):
            for length in LENGTHS:
                db.session.add(BendToolSegment(tool=tool, length_mm=length, erp_number=_next_erp_number()))
                db.session.flush()  # the next ERP number counts this one
                segs += 1
    db.session.commit()
    return dies, segs


if __name__ == '__main__':
    with app.app_context():
        print('Инструменти: %d, сегменти: %d' % seed())
