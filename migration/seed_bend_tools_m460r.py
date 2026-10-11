"""
One-off seed: the shop's own die set M460.r (4 dies, V 16/22/35/50 mm, 85 deg, die radius 2/2/2/3 mm, max 800 kN/m),
3 sets, each set with segments 10, 15, 20, 40, 50, 100, 200 and 370 mm. Tools go straight into the library; every
segment gets its own ERP number. Idempotent: a die that already exists is left alone.

    python -m migration.seed_bend_tools_m460r
"""
from app import BendTool, BendToolSegment, _next_erp_number, app, db

DIES = [(16, 2), (22, 2), (35, 2), (50, 3)]  # (V opening, die radius)
LENGTHS = [10, 15, 20, 40, 50, 100, 200, 370]
SETS = 3


def seed():
    """Returns (dies created, segments created)."""
    dies = segs = 0
    for v, r in DIES:
        code = f'M460.r V{v}'
        if BendTool.query.filter_by(code=code).first():
            continue
        tool = BendTool(code=code, kind='die', angle_deg=85, radius_mm=r, v_opening_mm=v, max_kn_per_m=800, status='in_library')
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
        print('Матрици: %d, сегменти: %d' % seed())
