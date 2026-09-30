"""
pytest: the "Материал на клиента" option - a line priced with the client's own
material drops the material cost and adds Client.own_material_surcharge_percent
on every service cost (Detail operations, product details, DXF cutting time).

Run with:
    pytest testing/test_own_material.py -v
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _safe_remove():
    try:
        os.remove(_db_path)
    except OSError:
        pass


atexit.register(_safe_remove)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Service, Detail, Operation, Product,
                 ProductDetail, ProductExtraCost, OrderItem, limiter,
                 detail_price_for, calculate_product_pricing, calculate_cnc_price_multi_service)


@pytest.fixture
def setup():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        mat = MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100,
                            cutting_speed_mm_per_min=1000, pierce_rate_per_min=60)
        client = Client(name='Клиент А', own_material_surcharge_percent=20)
        svc = Service(name='Рязане', price_per_hour_eur=60)
        db.session.add_all([mat, client, svc])
        db.session.flush()
        detail = Detail(name='Пластина', material_key='steel', width=1000, height=1000, total_length=40,
                        pierce_count=1, calculated_price=100)  # 1 m2 x 100 = material cost
        db.session.add(detail)
        db.session.flush()
        db.session.add(Operation(detail_id=detail.id, service_id=svc.id, duration_minutes=60))  # = 60 EUR
        product = Product(name='Стойка', markup_percent=0)
        db.session.add(product)
        db.session.flush()
        db.session.add_all([ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2),
                            ProductExtraCost(product_id=product.id, label='Боя', amount=10)])
        pw = generate_password_hash('irrelevant123')
        db.session.add(User(username='usr', password=pw, role='regular_user', client_id=client.id))
        db.session.commit()
        yield dict(client=client, svc=svc, detail=detail, product=product)
        db.session.remove()
        db.drop_all()


def test_detail_and_product_prices(setup):
    s = setup
    assert detail_price_for(s['detail'], s['client']) == 160                       # 100 material + 60 op
    assert detail_price_for(s['detail'], s['client'], own_material=True) == 72     # 60 op x 1.20
    assert detail_price_for(s['detail'], None, own_material=True) == 60            # no client -> no surcharge
    assert calculate_product_pricing(s['product'])['sell_price'] == 330            # 2 x 160 + 10
    assert calculate_product_pricing(s['product'], s['client'], True)['sell_price'] == 156  # 2 x 72 + 10 x 1.20


def test_dxf_price_own_material(setup):
    s = setup
    # 100 material; 1000mm cut = 1 min + 1 pierce (1/60 min) at 1 EUR/min = 1.02; setup fee 5
    normal = calculate_cnc_price_multi_service(1000, 1000, 1000, 1, 'steel', [s['svc'].id])
    own = calculate_cnc_price_multi_service(1000, 1000, 1000, 1, 'steel', [s['svc'].id], client=s['client'],
                                            own_material=True)
    assert normal == 106.02
    assert own == 7.22  # (1.0167 + 5) x 1.20, no material


def test_order_line_frozen_with_own_material(setup):
    s = setup
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'usr', 'password': 'irrelevant123'})
    c.post('/orders/new', data={
        'customer_name': 'X',
        'cart_json': f'[{{"type": "detail", "id": {s["detail"].id}, "qty": 1, "own_material": true}},'
                     f' {{"type": "product", "id": {s["product"].id}, "qty": 1}}]'})
    items = {i.detail_id is not None: i for i in OrderItem.query.all()}
    assert items[True].own_material and items[True].unit_price == 72
    assert not items[False].own_material and items[False].unit_price == 330
    assert 'материал на клиента' in items[True].item_name
