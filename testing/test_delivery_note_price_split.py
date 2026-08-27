"""
pytest regression test for _find_or_create_delivery_target()'s material
branch - specifically the price-lot split added so the production wizard
(admin_production_orders()) can offer a choice between price lots of "the
same" material. A delivery-note line for an EXISTING material (matched by
name/brand/dims/type) used to silently ignore whatever price was entered on
that line and just pool onto the existing row - this locks in the fix: a
different price now clones a brand-new row (same descriptive fields, new
cost_per_m2) instead, per the "keep separate items separate" rule already
documented on the function. No price entered, or the same price, still
reuses the existing row - a routine restock must not fragment the catalog.

Run with:
    pytest testing/test_delivery_note_price_split.py -v
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked as long as SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest

from app import app as flask_app, db, MaterialPrice, _find_or_create_delivery_target


@pytest.fixture
def ctx():
    flask_app.config.update(TESTING=True)
    with flask_app.app_context():
        db.create_all()
        original = MaterialPrice(
            key='sheet_steel_2mm', display_name='Стомана 2мм', cost_per_m2=10.0,
            cutting_speed_mm_per_min=2000, pierce_rate_per_min=30, type='sheets',
            thickness_mm=2.0, sheet_width_mm=1000, sheet_length_mm=2000,
        )
        db.session.add(original)
        db.session.commit()
        yield original.id
        db.session.remove()
        db.drop_all()


def test_no_price_reuses_existing_row(ctx):
    target = _find_or_create_delivery_target(
        'material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, None, material_type='sheets')
    assert target.id == ctx


def test_same_price_reuses_existing_row(ctx):
    target = _find_or_create_delivery_target(
        'material', 'Стомана 2мм', None, 1000, 2000, 2.0, 10.0, None, material_type='sheets')
    assert target.id == ctx


def test_different_price_creates_new_price_lot(ctx):
    target = _find_or_create_delivery_target(
        'material', 'Стомана 2мм', None, 1000, 2000, 2.0, 12.5, None, material_type='sheets')
    assert target.id != ctx, "a different price must not pool onto the existing row"
    assert target.cost_per_m2 == 12.5
    assert target.display_name == 'Стомана 2мм'
    assert target.thickness_mm == 2.0
    assert target.cutting_speed_mm_per_min == 2000
    assert target.pierce_rate_per_min == 30
    assert target.stock_quantity == 0.0, "the new lot starts with no stock of its own"

    same_rows = MaterialPrice.query.filter_by(display_name='Стомана 2мм', type='sheets').count()
    assert same_rows == 2, "both price lots stay selectable as distinct catalog rows"
