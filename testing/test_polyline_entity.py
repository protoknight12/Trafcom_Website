"""
Guards process_entity() against old-style POLYLINE entities (dxftype
'POLYLINE', not 'LWPOLYLINE'). ezdxf's Polyline class has no get_points() -
calling it raised AttributeError, silently swallowed by process_entity's
bare except, so any drawing using this entity type analyzed as empty
geometry (zero length, blank shapes/preview). LibreCAD/older CAD exports and
at least one real customer file use this entity type instead of LWPOLYLINE.

Run with:
    python -m testing.test_polyline_entity
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

import ezdxf

from app import process_entity

doc = ezdxf.new('R2000')
msp = doc.modelspace()
# Old-style 2D POLYLINE (not LWPOLYLINE) - a 100x50 closed rectangle.
polyline = msp.add_polyline2d([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
polyline.dxf.flags |= polyline.CLOSED

length, segments, shapes = process_entity(polyline)

assert length == 300.0, f"expected perimeter 300.0, got {length}"
assert len(segments) == 4, f"expected 4 segments, got {len(segments)}"
assert len(shapes) == 4, f"expected 4 line shapes, got {len(shapes)}"
assert all(s['type'] == 'line' for s in shapes)

print("ok")
