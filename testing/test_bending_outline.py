"""bending.outline.loops_from_shapes(): contours from the cut entities. Run: python -m testing.test_bending_outline"""
from bending.outline import loops_from_shapes

L = lambda a, b, c, d: {'type': 'line', 'x1': a, 'y1': b, 'x2': c, 'y2': d}
rect = [L(0, 0, 50, 0), L(50, 0, 50, 30), L(30, 30, 50, 30), L(30, 30, 0, 30), L(0, 30, 0, 0)]      # drawn in no particular order or direction
hole = {'type': 'circle', 'cx': 25, 'cy': 15, 'r': 3}
loops = loops_from_shapes(rect + [hole])
assert len(loops) == 2 and len(loops[0]) == 5 and len(loops[1]) >= 12          # the outline first, then the hole
assert {tuple(p) for p in loops[0]} == {(0, 0), (50, 0), (50, 30), (30, 30), (0, 30)}
# a rounded corner (a quarter arc) is part of the same loop
rounded = [L(5, 0, 50, 0), L(50, 0, 50, 30), L(50, 30, 0, 30), L(0, 30, 0, 5), {'type': 'arc', 'cx': 5, 'cy': 5, 'r': 5, 'start_angle': 180, 'end_angle': 270}]
(one,) = loops_from_shapes(rounded)
assert len(one) > 8 and min(p[0] for p in one) == 0 and abs(min(p[1] for p in one)) < 1e-9
assert loops_from_shapes([L(0, 0, 10, 0), L(10, 0, 10, 10)]) == []              # an open chain is not a contour
print('ok')

# a tapered part: the right edge runs from c = 100 at w = 0 to c = 80 at w = 200; the bend line is slanted too
from bending.outline import extent_at, line_c_at
taper = [(0, 0), (100, 0), (80, 200), (0, 200)]
assert extent_at(taper, 0) == (0, 100) and extent_at(taper, 100) == (0, 90) and extent_at(taper, 150) == (0, 85) and extent_at(taper, 250) is None
assert line_c_at([40, 0, 30, 200], 100) == 35 and line_c_at([40, 0, 30, 200], 0) == 40 and line_c_at([40, 0, 40, 0], 5) == 40
print('ok extent')
