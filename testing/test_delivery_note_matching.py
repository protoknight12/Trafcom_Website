"""Self-check for the delivery-note find-or-create matching rule
(_find_or_create_delivery_target) and the order fulfillment check
(order_missing_items) - see CLAUDE.md order-fulfillment / delivery-note
tasks. Uses a throwaway file-based SQLite DB, same trick as
test_security_fixes.py, since both functions need a real app/db context.

    python -m testing.test_delivery_note_matching
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

from werkzeug.security import generate_password_hash

from app import (
    app, db, MaterialPrice, Detail, Product, Order, OrderItem, User,
    _find_or_create_delivery_target, order_missing_items,
)

with app.app_context():
    db.create_all()

    # -- Material: identical params reuse the same row, any one differing
    #    field creates a new one -------------------------------------------
    m1 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, None,
                                          cost_per_m2=40.0, cost_per_meter_cut=2.0, cost_per_pierce=0.2)
    db.session.commit()
    m2 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, None,
                                          cost_per_m2=40.0, cost_per_meter_cut=2.0, cost_per_pierce=0.2)
    assert m1.id == m2.id, "identical material params must reuse the same catalog row"

    m3 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 3.0, None, None,  # different thickness
                                          cost_per_m2=40.0, cost_per_meter_cut=2.0, cost_per_pierce=0.2)
    db.session.commit()
    assert m3.id != m1.id, "a differing thickness must create a separate material row"
    assert MaterialPrice.query.count() == 2

    # -- Material: a brand-new material without full pricing must be refused
    #    rather than silently created with cost_per_m2=0.0 etc. (would
    #    silently produce €0.00 CNC prices everywhere it's later picked) ----
    no_price = _find_or_create_delivery_target('material', 'Титан Grade 5', None, None, None, None, None, None)
    assert no_price is None, "a new material with no pricing must be refused, not created zero-priced"
    assert MaterialPrice.query.filter_by(display_name='Титан Grade 5').first() is None
    partial_price = _find_or_create_delivery_target(
        'material', 'Титан Grade 5', None, None, None, None, None, None, cost_per_m2=100.0
    )
    assert partial_price is None, "all three of cost_per_m2/cost_per_meter_cut/cost_per_pierce are required, not just one"

    # -- Detail: needs a material_key; price is part of the match, so a
    #    different unit_price must NOT bump the old row's stock -----------
    mat = MaterialPrice.query.filter_by(display_name='Ламарина DC01', thickness_mm=2.0).first()
    d1 = _find_or_create_delivery_target('detail', 'Планка А', None, 100.0, 50.0, None, 12.5, mat.key)
    db.session.commit()
    d2 = _find_or_create_delivery_target('detail', 'Планка А', None, 100.0, 50.0, None, 12.5, mat.key)
    assert d1.id == d2.id, "identical detail params (incl. price) must reuse the same row"

    d3 = _find_or_create_delivery_target('detail', 'Планка А', None, 100.0, 50.0, None, 15.0, mat.key)  # different price
    db.session.commit()
    assert d3.id != d1.id, "a differing price must create a separate detail row, never merge stock"
    assert Detail.query.count() == 2

    no_material = _find_or_create_delivery_target('detail', 'Планка Б', None, 10.0, 10.0, None, 1.0, 'not-a-real-key')
    assert no_material is None, "an unknown material_key must refuse to create a detail (hard FK)"

    no_detail_price = _find_or_create_delivery_target('detail', 'Планка В', None, 10.0, 10.0, None, None, mat.key)
    assert no_detail_price is None, "a new detail with no unit_price must be refused, not created at price 0.00"

    # -- Product: bare-bones, matches on name only -------------------------
    p1 = _find_or_create_delivery_target('product', 'Готова кутия', None, None, None, None, 20.0, None)
    db.session.commit()
    p2 = _find_or_create_delivery_target('product', 'Готова кутия', None, None, None, None, 999.0, None)
    assert p1.id == p2.id, "products dedupe on name only (no dims/brand/price columns of their own)"
    assert Product.query.count() == 1

    # -- order_missing_items: shortfall until stock covers the order -------
    d1.stock_quantity = 2.0
    user = User(username='tester', password=generate_password_hash('irrelevant123'))
    db.session.add(user)
    db.session.commit()

    order = Order(order_number='ORD-TEST-0001', user_id=user.id, customer_name='Test', status='new')
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, detail_id=d1.id, quantity_ordered=5, unit_price=12.5))
    db.session.commit()

    shortfalls = order_missing_items(order)
    assert len(shortfalls) == 1 and shortfalls[0]['missing'] == 3, "5 ordered - 2 in stock = 3 missing"

    d1.stock_quantity = 5.0
    db.session.commit()
    assert order_missing_items(order) == [], "once stock covers the order, nothing should be flagged"

print("ok")
