"""
One-off import: exact punch outlines from the Delem controller's tool files (*.dld, XML; e.g. the machine's "PARAMETERS" backup) into the tool
library. Replaces the profile of an existing punch (same code, case-insensitive) or creates the punch. The controller's outline includes the
clamping part: it is cut off at the seat (height - tip offset), the standard tang is drawn by bending.profile.clamp_polygon. Dies are not
imported: the controller models every V of the M460.r block as a separate 80 mm die, while the library keeps the real 60 x 60 four-way block.

    python -m migration.import_delem_tools "<folder with pun.*.dld>"

Safe to run more than once.
"""
import glob
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

import ezdxf

from app import BendTool, app, db
from bending.profile import clip_to_seat, derived, polygon_from_dxf, tip_offset, validate_polygon


def dld_to_doc(path):
    """-> (ezdxf doc with the cross-section as LINE / ARC entities, tool name, apex-based height). Segment: x1 y1 x2 y2 false | x1 y1 x2 y2 true cw cx cy."""
    root = ET.parse(path).getroot()
    val = lambda tag: root.find('.//' + tag).get('value')
    t = val('ToolCrossSection').split()
    n, f, i = int(t[0]), t[1:], 0
    doc = ezdxf.new()
    msp = doc.modelspace()
    for _ in range(n):
        x1, y1, x2, y2 = map(float, f[i:i + 4])
        if f[i + 4] == 'false':
            msp.add_line((x1, y1), (x2, y2))
            i += 5
            continue
        cw, cx, cy = f[i + 5] == 'true', float(f[i + 6]), float(f[i + 7])
        i += 8
        a1, a2 = math.degrees(math.atan2(y1 - cy, x1 - cx)), math.degrees(math.atan2(y2 - cy, x2 - cx))
        msp.add_arc((cx, cy), math.hypot(x1 - cx, y1 - cy), *((a2, a1) if cw else (a1, a2)))
    return doc, val('ToolName'), float(val('ToolHeight'))


def tip_arc_to_fillet(pts):
    """The controller draws the rounded tip as a short arc between the flanks; the library keeps it as a corner fillet (radius = data, not a drawing)."""
    n = len(pts)
    i = min((k for k in range(n) if pts[k][3] and math.hypot(pts[k][0] - pts[(k + 1) % n][0], pts[k][1] - pts[(k + 1) % n][1]) < 3.0), key=lambda k: pts[k][1], default=None)
    if i is None:
        return pts
    a, b, prev, nxt = pts[i], pts[(i + 1) % n], pts[i - 1], pts[(i + 2) % n]
    d1, d2 = (a[0] - prev[0], a[1] - prev[1]), (b[0] - nxt[0], b[1] - nxt[1])   # flank lines through prev -> a and nxt -> b
    det = d1[0] * -d2[1] + d1[1] * d2[0]
    if abs(det) < 1e-9:
        return pts
    t = ((nxt[0] - prev[0]) * -d2[1] + (nxt[1] - prev[1]) * d2[0]) / det
    apex = [round(prev[0] + d1[0] * t, 3), round(prev[1] + d1[1] * t, 3), abs(a[3]), 0.0]
    rest = [q for k, q in enumerate(pts) if k not in (i, (i + 1) % n)]
    j = rest.index(prev) + 1
    return rest[:j] + [apex] + rest[j:]


def run(folder):
    for path in sorted(glob.glob(os.path.join(folder, 'pun.*.dld'))):
        doc, name, height = dld_to_doc(path)
        pts = tip_arc_to_fillet(polygon_from_dxf(doc, 'punch'))
        tool = BendTool.query.filter(db.func.lower(BendTool.code) == name.lower(), BendTool.kind == 'punch').first()
        angle, radius = (tool.angle_deg, tool.radius_mm) if tool else (None, None)
        if not tool:
            d = derived(pts, 'punch')
            angle, radius = round(d.get('tip_angle'), 1), round(d.get('tip_radius'), 2)
        work_h = round(height - 30.0, 2)   # catalogue heights are to the theoretical apex, the standard clamp is 30 mm
        pts = validate_polygon(clip_to_seat(pts, round(work_h - tip_offset(angle, radius), 2)))
        if not tool:
            tool = BendTool(code=name, kind='punch', height_mm=work_h, angle_deg=angle, radius_mm=radius, status='in_library')
            db.session.add(tool)
        tool.profile_json = json.dumps(pts)
        print(f'{name}: {len(pts)} points, height {work_h}, tip {angle}/{radius}')
    db.session.commit()


if __name__ == '__main__':
    with app.app_context():
        run(sys.argv[1])
