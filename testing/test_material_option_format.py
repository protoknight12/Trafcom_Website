"""Self-check for format_material_option() - the standardized
"#ID Name (Brand, Width mm, Length mm, Thickness mm)" text used by every
material <select> in the app. Guards null handling: any of brand/width/
length/thickness may be blank, in which case that slot shows "-" - the
option always has all four comma-separated slots, never a shorter or
missing parenthesized part. Also guards the "#ID " prefix itself: present
when material.id is set, omitted (not "#None ") for an unsaved row.

    python -m testing.test_material_option_format
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from app import format_material_option, MaterialPrice


def _material(**kwargs):
    defaults = dict(
        key='k', display_name='Алуминий', cost_per_m2=0, cutting_speed_mm_per_min=0, pierce_rate_per_min=0,
        sheet_length_mm=None, sheet_width_mm=None, thickness_mm=None, brand=None,
    )
    defaults.update(kwargs)
    return MaterialPrice(**defaults)


full = _material(id=5, brand='Armiko', sheet_width_mm=10, sheet_length_mm=20, thickness_mm=2)
assert format_material_option(full) == '#5 Алуминий (Armiko, 10mm, 20mm, 2mm)', format_material_option(full)

no_dims = _material(id=5, brand='Armiko')
assert format_material_option(no_dims) == '#5 Алуминий (Armiko, -, -, -)', format_material_option(no_dims)

no_brand = _material(id=5, sheet_width_mm=10, sheet_length_mm=20, thickness_mm=2)
assert format_material_option(no_brand) == '#5 Алуминий (-, 10mm, 20mm, 2mm)', format_material_option(no_brand)

bare = _material(id=5)
assert format_material_option(bare) == '#5 Алуминий (-, -, -, -)', format_material_option(bare)

partial_dims = _material(id=5, brand='Armiko', thickness_mm=2.5)
assert format_material_option(partial_dims) == '#5 Алуминий (Armiko, -, -, 2.5mm)', format_material_option(partial_dims)

unsaved = _material()
assert format_material_option(unsaved) == 'Алуминий (-, -, -, -)', format_material_option(unsaved)

print("ok")
