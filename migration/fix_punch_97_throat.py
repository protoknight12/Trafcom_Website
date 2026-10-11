"""
One-off data fix: the punch P.97.75.r08 had a straight right side; its drawing (97.60 / 67.60 / 26.25, 75 deg, R0.8) has a concave throat there, a ~R27 arc
from just above the tip flank (y 6.9) up to y 43.9 on the right side, leftmost point ~1.5 mm past the tip axis. Sets that arc as the outline's `a` value.

    python -m migration.fix_punch_97_throat

Safe to run more than once; only touches a P.97.75.r08 whose profile still has the old straight edge.
"""
import json

from app import app, db, BendTool

with app.app_context():
    t = BendTool.query.filter_by(code='P.97.75.r08', kind='punch').first()
    pts = t.profile if t else None
    if not pts:
        print('P.97.75.r08 not found or without a profile - nothing to do.')
    else:
        i = next((k for k, p in enumerate(pts) if abs(p[0] - 5.67) < 0.01 and abs(p[1] - 6.87) < 0.01), None)
        if i is None or pts[i][3]:
            print('already fixed (or a different outline) - nothing to do.')
        else:
            pts[i][3] = 27.0
            nxt = pts[(i + 1) % len(pts)]
            nxt[1] = 43.9   # the throat arc ends on the right side at y 43.9 (was 41.08)
            t.profile_json = json.dumps(pts)
            db.session.commit()
            print('P.97.75.r08: throat arc R27 added.')
