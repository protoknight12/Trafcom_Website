"""
pytest guard against N+1 queries on the list pages: the number of SELECTs a page
needs must not grow with the number of rows it lists (eager loading via
selectinload/joinedload on the list query).

Seeds a few parents with children, renders each page through the test client with a
`before_cursor_execute` listener counting SELECTs, then adds more parents and counts
again. The check is "statements for 10 rows <= statements for 2 rows + small constant".

Run with:
    python -m pytest testing/test_query_counts.py -q -p no:cacheprovider
"""
import os
import tempfile
from datetime import date, datetime, timedelta

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, limiter, User, MaterialPrice, Detail, Product, ProductDetail,
                 ProductExtraCost, Operation, Service, Client, Supplier, Order, OrderItem,
                 OrderItemComponent, OrderItemAttachment, Offer, OfferItem, DeliveryNote,
                 DeliveryNoteItem, ClientDeliveryNote, ClientDeliveryNoteItem, Vehicle,
                 VehicleInsuranceInstallment, ProductionOrder)

SLACK = 3   # allowed growth in statements between 2 and 10 rows


def _seed_one(i, admin_id, material, services):
    """One 'row' of everything every list page shows, with children."""
    client = Client(name=f'Клиент {i}')
    supplier = Supplier(name=f'Доставчик {i}')
    db.session.add_all([client, supplier])
    db.session.flush()
    own_material = MaterialPrice(key=f'qc_mat_{i}', display_name=f'QC Material {i}', cost_per_m2=10,
                                 cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
    db.session.add(own_material)   # a material per row, so a per-row lazy load of Detail.material shows up
    details = []
    for k in range(2):
        d = Detail(name=f'Детайл {i}-{k}', material_key=own_material.key, width=10, height=10, total_length=1,
                   pierce_count=1, calculated_price=5.0, stock_quantity=0, owner_client_id=client.id,
                   geometry_json='[]')
        db.session.add(d)
        db.session.flush()
        for s_idx, svc in enumerate(services):
            db.session.add(Operation(detail_id=d.id, service_id=svc.id, sequence=s_idx, duration_minutes=3))
        details.append(d)
    for status in ('pending', 'done'):
        db.session.add(ProductionOrder(detail_id=details[0].id, material_id=own_material.id, quantity=2,
                                       planned_material_qty=1.0, status=status, created_by_id=admin_id,
                                       completed_by_id=admin_id if status == 'done' else None,
                                       completed_at=datetime.utcnow() if status == 'done' else None))
    product = Product(name=f'Продукт {i}', markup_percent=10, owner_client_id=client.id)
    db.session.add(product)
    db.session.flush()
    for d in details:
        db.session.add(ProductDetail(product_id=product.id, detail_id=d.id, quantity=2))
    db.session.add(ProductExtraCost(product_id=product.id, label='Боя', amount=4))

    order = Order(order_number=f'ORD-{i}', user_id=admin_id, customer_name=f'Клиент {i}', status='new',
                  client_id=client.id)
    db.session.add(order)
    db.session.flush()
    p_item = OrderItem(order_id=order.id, product_id=product.id, quantity_ordered=5, unit_price=9)
    d_item = OrderItem(order_id=order.id, detail_id=details[0].id, quantity_ordered=3, unit_price=7)
    db.session.add_all([p_item, d_item])
    db.session.flush()
    for d in details:
        db.session.add(OrderItemComponent(order_item_id=p_item.id, detail_id=d.id, detail_name_snapshot=d.name,
                                          quantity_needed=10))
    db.session.add(OrderItemAttachment(order_item_id=d_item.id, filename=f'f{i}.pdf', original_filename='f.pdf'))

    offer = Offer(number=f'{i:011d}', object_title=f'Обект {i}', client_id=client.id)
    db.session.add(offer)
    db.session.flush()
    for pos in range(3):
        db.session.add(OfferItem(offer_id=offer.id, position=pos, item_type='text', name=f'ред {pos}',
                                 quantity=1, unit_price=2))

    note = DeliveryNote(supplier_id=supplier.id)
    cnote = ClientDeliveryNote(client_id=client.id)
    db.session.add_all([note, cnote])
    db.session.flush()
    for pos in range(2):
        db.session.add(DeliveryNoteItem(delivery_note_id=note.id, target_type='material', target_id=material.id,
                                        description_snapshot='мат', quantity=1))
        db.session.add(ClientDeliveryNoteItem(client_delivery_note_id=cnote.id, target_type='detail',
                                              target_id=details[0].id, description_snapshot='дет', quantity=1))
    veh = Vehicle(name=f'Кола {i}', license_plate=f'CB{i:04d}XX', insurance_installments=True,
                  insurance_expiry=date.today() + timedelta(days=60), vignette_expiry=date.today() - timedelta(days=3))
    db.session.add(veh)
    db.session.flush()
    db.session.add(VehicleInsuranceInstallment(vehicle_id=veh.id, due_date=date.today(), paid=False))


@pytest.fixture(scope='module')
def env():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qc_admin', password=generate_password_hash('irrelevant123'), role='admin')
        material = MaterialPrice(key='qc_mat', display_name='QC Material', cost_per_m2=10,
                                 cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        services = [Service(name=f'Услуга {n}', price_per_hour_eur=30) for n in range(3)]
        db.session.add_all([admin, material] + services)
        db.session.commit()
        state = {'admin_id': admin.id, 'material_id': material.id, 'seeded': 0}

    def grow_to(n):
        if n <= state['seeded']:
            return
        with flask_app.app_context():
            material = db.session.get(MaterialPrice, state['material_id'])
            services = Service.query.all()
            for i in range(state['seeded'], n):
                _seed_one(i, state['admin_id'], material, services)
            db.session.commit()
        state['seeded'] = n

    state['grow_to'] = grow_to
    yield state
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def _count_selects(url):
    """SELECT statements issued while one logged-in client GETs `url`, login excluded."""
    limiter.reset()
    c = flask_app.test_client()
    assert c.post('/login', data={'username': 'qc_admin', 'password': 'irrelevant123'}).status_code in (200, 302)
    stmts = []

    def on_exec(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            stmts.append(statement)

    with flask_app.app_context():
        engine = db.engine   # fetched here, the context is gone again before the request (own session per request)
    event.listen(engine, 'before_cursor_execute', on_exec)
    try:
        res = c.get(url)
    finally:
        event.remove(engine, 'before_cursor_execute', on_exec)
    assert res.status_code == 200, (url, res.status_code)
    return len(stmts)


PAGES = ['/orders', '/admin/production', '/admin/missing-stock', '/admin/offers', '/admin/products',
         '/admin/details', '/admin/delivery-notes', '/admin/client-delivery-notes', '/admin/materials/1/history',
         '/storage/details', '/storage/products', '/storage/materials', '/admin/production-orders',
         '/admin/offers/new', '/admin/offers/1/edit']


@pytest.fixture(scope='module')
def counts(env):
    """{url: (SELECTs with 2 rows, SELECTs with 10 rows)} - every page measured at 2 rows first, then at 10."""
    env['grow_to'](2)
    small = {url: _count_selects(url) for url in PAGES}
    env['grow_to'](10)
    return {url: (small[url], _count_selects(url)) for url in PAGES}


@pytest.mark.parametrize('url', PAGES)
def test_select_count_does_not_grow_with_rows(counts, url):
    small, big = counts[url]
    print(f'{url}: 2 rows -> {small} SELECTs, 10 rows -> {big} SELECTs')
    assert big <= small + SLACK, f'{url}: {small} SELECTs for 2 rows but {big} for 10 (N+1)'


def test_vehicle_alerts_output_unchanged_and_cheap(env):
    """inject_vehicle_alerts() banner: same alerts as Vehicle.deadlines/next_insurance_installment give."""
    env['grow_to'](4)   # (no-op when the rows above already exist)
    with flask_app.test_request_context('/'):
        from app import inject_vehicle_alerts
        # not authenticated -> no alerts at all
        assert inject_vehicle_alerts() == {'vehicle_alerts': []}
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qc_admin', 'password': 'irrelevant123'})
    html = c.get('/admin/offers').get_data(as_text=True)
    # every seeded vehicle has an expired vignette and an unpaid installment due today -> two banner entries each
    assert html.count('Винетка') == env['seeded']
    assert html.count('Вноска ГО') == env['seeded']
