"""Self-check for _material_cost()'s per-type branching - guards a real
production bug: pipes were falling into the area branch (width * height,
i.e. diameter * length) instead of being priced by length like rods, because
only 'rods' was special-cased. A pipe's "width" is its outer diameter (see
DETAIL_DIMENSION_LABELS/MATERIAL_DIMENSION_LABELS in app.py), not a literal
width, so diameter * length is not a real area - pipes are round stock
bought and cut by the linear meter, same as rods. Profiles are priced the
same way (bar stock bought/cut by length, cross-section width ignored) -
sheets are the only remaining genuinely area-priced type.

    python -m testing.test_material_cost_by_type
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from app import _material_cost, MaterialPrice


def _material(type_, cost_per_m2=10.0):
    return MaterialPrice(key='k', display_name='QA', type=type_, cost_per_m2=cost_per_m2,
                          cutting_speed_mm_per_min=1, pierce_rate_per_min=1)


# Sheets: genuine area (width * height / 1e6) * cost_per_m2.
sheet = _material('sheets')
assert _material_cost(1000, 500, sheet) == 5.0, _material_cost(1000, 500, sheet)  # 0.5 m^2 * 10

# Profiles: length (height) only, same as rods/pipes - cross-section width ignored.
# (width=2000 deliberately chosen so the old area formula would give 10.0,
# not 5.0 - a regression back to area-pricing would fail this assertion.)
profile = _material('profiles')
assert _material_cost(2000, 500, profile) == 5.0, _material_cost(2000, 500, profile)  # 0.5 m * 10 EUR/m
# A wide short profile and a narrow long profile of the same length must cost the same -
# cross-section width must not factor in at all.
assert _material_cost(2000, 500, profile) == _material_cost(40, 500, profile)

# Rods: length (height) only, cost_per_m2 is really EUR/linear meter - width (diameter) ignored.
rod = _material('rods')
assert _material_cost(20, 2000, rod) == 20.0, _material_cost(20, 2000, rod)  # 2 m * 10 EUR/m, diameter irrelevant

# Pipes: same as rods - round stock priced by length, "width" is outer diameter, not a real area side.
pipe = _material('pipes')
assert _material_cost(60, 3000, pipe) == 30.0, _material_cost(60, 3000, pipe)  # 3 m * 10 EUR/m
# A fat short pipe and a thin long pipe of the same length must cost the same -
# diameter must not factor in at all (the bug this test guards).
assert _material_cost(200, 3000, pipe) == _material_cost(10, 3000, pipe)

print("ok")
