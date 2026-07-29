"""Self-check for format_material_option() - the standardized
"Name (Brand, Width mm, Length mm, Thickness mm)" text used by every
material <select> in the app. Guards null handling: any of brand/width/
length/thickness may be blank, in which case that slot shows "-" - the
option always has all four comma-separated slots, never a shorter or
missing parenthesized part.

    python -m testing.test_material_option_format
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from app import format_material_option, MaterialPrice


def _material(**kwargs):
    defaults = dict(
        key='k', display_name='Алуминий', cost_per_m2=0, cost_per_meter_cut=0, cost_per_pierce=0,
        sheet_length_mm=None, sheet_width_mm=None, thickness_mm=None, brand=None,
    )
    defaults.update(kwargs)
    return MaterialPrice(**defaults)


full = _material(brand='Armiko', sheet_width_mm=10, sheet_length_mm=20, thickness_mm=2)
assert format_material_option(full) == 'Алуминий (Armiko, 10mm, 20mm, 2mm)', format_material_option(full)

no_dims = _material(brand='Armiko')
assert format_material_option(no_dims) == 'Алуминий (Armiko, -, -, -)', format_material_option(no_dims)

no_brand = _material(sheet_width_mm=10, sheet_length_mm=20, thickness_mm=2)
assert format_material_option(no_brand) == 'Алуминий (-, 10mm, 20mm, 2mm)', format_material_option(no_brand)

bare = _material()
assert format_material_option(bare) == 'Алуминий (-, -, -, -)', format_material_option(bare)

partial_dims = _material(brand='Armiko', thickness_mm=2.5)
assert format_material_option(partial_dims) == 'Алуминий (Armiko, -, -, 2.5mm)', format_material_option(partial_dims)

print("ok")
