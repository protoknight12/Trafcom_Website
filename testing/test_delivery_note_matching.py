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
    app, db, MaterialPrice, Detail, Product, ProductDetail, Order, OrderItem, User,
    _find_or_create_delivery_target, order_missing_items,
)

with app.app_context():
    db.create_all()

    # -- Material: identical params reuse the same row, a differing
    #    name/dims/price/description creates a new one ----------------------
    m1 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, None,
                                          cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    db.session.commit()
    m2 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, None,
                                          cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    assert m1.id == m2.id, "identical material params must reuse the same catalog row"

    m3 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 3.0, None, None,  # different thickness
                                          cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    db.session.commit()
    assert m3.id != m1.id, "a differing thickness must create a separate material row"
    assert MaterialPrice.query.count() == 2

    # -- Material: brand/manufacturer and quantity are NOT matching fields
    #    anymore (boss revised the rule) - a different brand on an otherwise
    #    identical line must reuse m1, not split ------------------------------
    m_diff_brand = _find_or_create_delivery_target('material', 'Ламарина DC01', 'SomeOtherBrand', 1000.0, 2000.0, 2.0, None, None,
                                                     cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    assert m_diff_brand.id == m1.id, "a differing brand/manufacturer must NOT create a separate material row"
    assert MaterialPrice.query.count() == 2, "matching brand-only difference must not create a new row"

    # -- Material: a differing description (the delivery-note line's free
    #    "Описание" field) DOES split into a new row, even with identical
    #    name/dims/price ------------------------------------------------------
    m_diff_notes = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, None,
                                                     cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2,
                                                     notes='повредена опаковка')
    db.session.commit()
    assert m_diff_notes.id != m1.id, "a differing description must create a separate material row"
    assert MaterialPrice.query.count() == 3
    m_same_notes_again = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, None,
                                                           cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2,
                                                           notes='повредена опаковка')
    assert m_same_notes_again.id == m_diff_notes.id, "matching description must reuse that row, not m1"
    assert MaterialPrice.query.count() == 3

    # -- Material: a delivery-note line's unit_price is a SINGULAR price (one
    #    whole 1000x2000mm sheet), not an already-computed €/m² rate - m1 is
    #    40.0 €/m² over a 2m² sheet, so 80.0 € is the SAME lot (derives back
    #    to 40.0 €/m²) and must reuse m1, not split into a new row -----------
    same_lot = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, 80.0, None,
                                                 cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    assert same_lot.id == m1.id, "a singular price that derives back to the same €/m² must reuse the existing lot"
    assert MaterialPrice.query.count() == 3, "matching a singular price must not create a new row"

    # A genuinely different singular price (100.0 € over 2m² = 50.0 €/m²,
    # not 40.0) must split into a new price-lot row, and that new row must
    # keep the raw singular price too (price_per_unit), not just the
    # derived €/m² rate.
    new_lot = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, 100.0, None,
                                                cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    db.session.commit()
    assert new_lot.id != m1.id, "a singular price that derives to a different €/m² must create a new price-lot row"
    assert abs(new_lot.cost_per_m2 - 50.0) < 0.001, "cost_per_m2 must be derived from the singular price / area, not the raw singular price"
    assert new_lot.price_per_unit == 100.0, "the new row must keep the raw singular price entered on the delivery note"
    assert MaterialPrice.query.count() == 4

    # -- Material: m1 and new_lot are two price-lots of the SAME name/dims/
    #    brand/notes/type (that's the whole point of a price-lot) - a restock
    #    line picked from the dropdown carries material_key identifying
    #    EXACTLY which lot the admin selected, and a blank price (the normal
    #    routine-restock case) must bump that exact lot, never whichever lot
    #    a fields-only lookup happens to find first (this used to silently
    #    bump the wrong lot - see CLAUDE.md delivery-note task) -------------
    restock_new_lot = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, new_lot.key,
                                                        cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    assert restock_new_lot.id == new_lot.id, "a restock with material_key must bump exactly the lot that was picked, not the lowest-id lookalike"
    restock_m1 = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1000.0, 2000.0, 2.0, None, m1.key,
                                                   cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    assert restock_m1.id == m1.id, "picking the OTHER lookalike lot must bump that one instead, not new_lot"
    assert MaterialPrice.query.count() == 4, "neither restock should create a new row"

    # -- Material: a restock line whose material_key row no longer matches
    #    this line's own dims (the admin edited them away from the catalog
    #    default) must NOT bump that row - it creates the distinct variant
    #    row instead (see the "A line for an EXISTING catalog material..."
    #    fallback below) ------------------------------------------------------
    edited_variant = _find_or_create_delivery_target('material', 'Ламарина DC01', 'Alcoa', 1200.0, 2000.0, 2.0, None, new_lot.key,
                                                       cost_per_m2=40.0, cutting_speed_mm_per_min=2.0, pierce_rate_per_min=0.2)
    db.session.commit()
    assert edited_variant.id not in (m1.id, new_lot.id), "an edited-dims restock line must create its own distinct row, not bump either lookalike"
    assert MaterialPrice.query.count() == 5

    # -- Material #86 (owner's report 2026-11-21): a picked material that has a Забележка (notes) and a profile
    #    height, delivered with an empty "Описание" on the line, is THAT material - it takes the price in place;
    #    a 0 price on the line never makes a new price lot either.
    profile = MaterialPrice(key='prof86', display_name='Профил 100x80', notes='затворен профил', type='profiles',
                            sheet_width_mm=100.0, sheet_length_mm=6000.0, thickness_mm=4.0, height_mm=80.0,
                            cost_per_m2=0.0)
    db.session.add(profile)
    db.session.commit()
    picked = _find_or_create_delivery_target('material', 'Профил 100x80', None, 100.0, 6000.0, 4.0, 58.469, 'prof86',
                                             material_type='profiles')
    db.session.commit()
    assert picked.id == profile.id and picked.price_per_unit == 58.469, "the picked noted material takes the price"
    zero = _find_or_create_delivery_target('material', 'Профил 100x80', None, 100.0, 6000.0, 4.0, 0.0, 'prof86',
                                           material_type='profiles')
    assert zero.id == profile.id and profile.price_per_unit == 58.469, "a 0 price never splits nor overwrites"
    other = _find_or_create_delivery_target('material', 'Профил 100x80', None, 100.0, 6000.0, 4.0, 70.0, 'prof86',
                                            material_type='profiles')
    db.session.commit()
    assert other.id != profile.id and other.height_mm == 80.0 and other.notes == 'затворен профил', \
        "a new price lot keeps the profile height and the note"
    assert MaterialPrice.query.count() == 7

    # -- A receipt from a client of a material created just for it (ours, empty, no price, unused) makes it the
    #    client's - no empty twin; a second client gets its own copy, a used / priced lot is never taken over.
    from app import Client
    c1, c2 = Client(name='Клиент 1'), Client(name='Клиент 2')
    blank = MaterialPrice(key='blank', display_name='Лист DC01 3', type='sheets', sheet_width_mm=1000.0,
                          sheet_length_mm=3200.0, thickness_mm=3.0, cost_per_m2=0.0, stock_quantity=0.0)
    db.session.add_all([c1, c2, blank])
    db.session.commit()
    got = _find_or_create_delivery_target('material', 'Лист DC01 3', None, 1000.0, 3200.0, 3.0, None, 'blank',
                                          owner_client_id=c1.id)
    db.session.commit()
    assert got.id == blank.id and blank.owner_client_id == c1.id, "the empty unused lot becomes the client's"
    assert MaterialPrice.query.count() == 8
    got2 = _find_or_create_delivery_target('material', 'Лист DC01 3', None, 1000.0, 3200.0, 3.0, None, 'blank',
                                           owner_client_id=c2.id)
    db.session.commit()
    assert got2.id != blank.id and got2.owner_client_id == c2.id and blank.owner_client_id == c1.id
    assert MaterialPrice.query.count() == 9
    got3 = _find_or_create_delivery_target('material', 'Профил 100x80', None, 100.0, 6000.0, 4.0, None, 'prof86',
                                           material_type='profiles', owner_client_id=c1.id)
    db.session.commit()
    assert got3.id != profile.id and profile.owner_client_id is None, "a priced lot of ours is never taken over"
    assert MaterialPrice.query.count() == 10

    # -- Material: a brand-new material with no price and no cutting speeds is
    #    created at 0 ("няма доставна цена"), like one from admin_materials.html -
    #    speeds belong to the detail / machine now (owner's decision 2026-11-20) --
    no_price = _find_or_create_delivery_target('material', 'Титан Grade 5', None, None, None, None, None, None)
    db.session.commit()
    assert no_price is not None and no_price.cost_per_m2 == 0
    assert no_price.cutting_speed_mm_per_min is None and no_price.pierce_rate_per_min is None
    db.session.delete(no_price)
    db.session.commit()

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

    # -- Product: an optional `components` dict attaches ProductDetail rows
    #    only when a brand-new product is created, never on a match --------
    p3 = _find_or_create_delivery_target(
        'product', 'Продукт с компоненти', None, None, None, None, None, None, components={d1.id: 2}
    )
    db.session.commit()
    rows = ProductDetail.query.filter_by(product_id=p3.id).all()
    assert len(rows) == 1 and rows[0].detail_id == d1.id and rows[0].quantity == 2

    p4 = _find_or_create_delivery_target(
        'product', 'Продукт с компоненти', None, None, None, None, None, None, components={d1.id: 99}
    )
    assert p4.id == p3.id, "identical name must still dedupe even when components are passed"
    assert ProductDetail.query.filter_by(product_id=p3.id).count() == 1, \
        "matching an existing product must never attach/duplicate components"

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
