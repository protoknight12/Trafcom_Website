"""
pytest: producing parts for a customer Order (admin_production_report()'s POST)
also takes the detail's raw material out of stock - _consume_detail_material(),
the same per-piece need and stock-unit conversion as the production wizard /
complete_production_order(). A correction downward returns it, a client's
stored lot may not go below zero (JSON refusal, nothing moved), our lots may,
a component-less product line and a "материал на клиента" line move no
material, the lot and the net taken are frozen on the order row
(material_consumed / consumed_material_id), and the material's history page lists
that frozen figure as 'order_production'.

Run with:
    pytest testing/test_order_production_material.py -v
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
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Product, ProductDetail, Order,
                 OrderItem, OrderItemComponent, limiter, seed_material_types,
                 _detail_material_unit_qty, _material_stock_delta)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        owner = Client(name='Клиент А')
        db.session.add_all([admin, owner])
        db.session.flush()
        ours = MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=10, type='sheets',
                             sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=10)
        lot = MaterialPrice(key='steel_c', display_name='Стомана 2мм', cost_per_m2=0, type='sheets',
                            sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=0.1, owner_client_id=owner.id)
        db.session.add_all([ours, lot])
        db.session.flush()
        # 500 x 400 mm = 0.2 m² per piece = 0.1 of a 2 m² sheet
        detail = Detail(name='Планка', material_key='steel', width=500, height=400, total_length=1,
                        pierce_count=1, calculated_price=5.0, stock_quantity=0)
        private = Detail(name='Планка клиент', material_key='steel_c', width=500, height=400, total_length=1,
                         pierce_count=1, calculated_price=0.0, stock_quantity=0, owner_client_id=owner.id)
        product = Product(name='Изделие', markup_percent=0)
        bare_product = Product(name='Без детайли', markup_percent=0)
        db.session.add_all([detail, private, product, bare_product])
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        order = Order(order_number='ORD-M-1', user_id=admin.id, customer_name='QA Клиент', status='new')
        db.session.add(order)
        db.session.flush()
        item = OrderItem(order_id=order.id, detail_id=detail.id, quantity_ordered=200, unit_price=5.0)
        private_item = OrderItem(order_id=order.id, detail_id=private.id, quantity_ordered=10, unit_price=1.0)
        product_item = OrderItem(order_id=order.id, product_id=product.id, quantity_ordered=5, unit_price=9.0)
        bare_item = OrderItem(order_id=order.id, product_id=bare_product.id, quantity_ordered=4, unit_price=9.0)
        db.session.add_all([item, private_item, product_item, bare_item])
        db.session.flush()
        component = OrderItemComponent(order_item_id=product_item.id, detail_id=detail.id,
                                       detail_name_snapshot=detail.name, quantity_needed=10)
        db.session.add(component)
        db.session.commit()
        yield dict(ours=ours.id, lot=lot.id, detail=detail.id, private=private.id, item=item.id,
                   private_item=private_item.id, component=component.id, bare_item=bare_item.id)
        db.session.remove()
        db.drop_all()


@pytest.fixture
def c(ids):
    client = flask_app.test_client()
    client.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return client


def _post(c, target_type, target_id, qty):
    return c.post('/admin/production', data={'target_type': target_type, 'target_id': target_id,
                                             'produced_qty': str(qty)})


def _stock(model, row_id):
    with flask_app.app_context():
        return db.session.get(model, row_id).stock_quantity


def _taken(detail_id, material_id, pieces):
    with flask_app.app_context():
        detail = db.session.get(Detail, detail_id)
        return _material_stock_delta(db.session.get(MaterialPrice, material_id),
                                     _detail_material_unit_qty(detail) * pieces)


def test_producing_a_standalone_detail_takes_its_material(c, ids):
    assert _post(c, 'item', ids['item'], 5).get_json()['status'] == 'success'
    assert _taken(ids['detail'], ids['ours'], 5) == pytest.approx(0.5)  # 5 x 0.2 m² = half a sheet
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(10 - 0.5)
    assert _stock(Detail, ids['detail']) == 5


def test_a_component_takes_material_and_a_correction_returns_it(c, ids):
    _post(c, 'component', ids['component'], 8)
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(10 - _taken(ids['detail'], ids['ours'], 8))
    _post(c, 'component', ids['component'], 3)
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(10 - _taken(ids['detail'], ids['ours'], 3))
    _post(c, 'component', ids['component'], 0)
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(10)
    with flask_app.app_context():   # exactly 0, not a float crumb - the history filter is `!= 0`
        assert db.session.get(OrderItemComponent, ids['component']).material_consumed == 0


def test_our_lot_may_go_negative(c, ids):
    assert _post(c, 'item', ids['item'], 200).get_json()['status'] == 'success'  # 20 sheets needed, 10 on hand
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(-10)


def test_client_lot_cannot_go_negative(c, ids):
    res = _post(c, 'item', ids['private_item'], 5)  # 0.5 sheet needed, the client left 0.1
    assert res.status_code == 400
    data = res.get_json()
    assert data['status'] == 'error' and 'клиентска партида' in data['message']
    assert _stock(MaterialPrice, ids['lot']) == pytest.approx(0.1)
    assert _stock(Detail, ids['private']) == 0
    with flask_app.app_context():
        assert db.session.get(OrderItem, ids['private_item']).quantity_produced == 0

    assert _post(c, 'item', ids['private_item'], 1).get_json()['status'] == 'success'  # exactly the 0.1 left
    assert _stock(MaterialPrice, ids['lot']) == pytest.approx(0, abs=1e-9)


def test_product_without_details_moves_no_material(c, ids):
    assert _post(c, 'item', ids['bare_item'], 4).get_json()['status'] == 'success'
    assert _stock(MaterialPrice, ids['ours']) == 10
    assert _stock(MaterialPrice, ids['lot']) == pytest.approx(0.1)


def _history(c, material_id):
    return c.get(f'/admin/materials/{material_id}/history').get_data(as_text=True)


def test_material_history_shows_the_frozen_figure(c, ids):
    _post(c, 'item', ids['item'], 5)
    _post(c, 'component', ids['component'], 2)
    with flask_app.app_context():
        item = db.session.get(OrderItem, ids['item'])
        assert item.consumed_material_id == ids['ours'] and item.material_consumed == pytest.approx(0.5)
        assert db.session.get(OrderItemComponent, ids['component']).material_consumed == pytest.approx(0.2)
    html = _history(c, ids['ours'])
    assert html.count('Производство по поръчка</span>') == 2
    assert 'ORD-M-1' in html and '5 бр. &#34;Планка&#34;' in html and '-0.5<' in html and '-0.2<' in html

    with flask_app.app_context():  # resizing the detail afterwards rewrites nothing
        db.session.get(Detail, ids['detail']).width = 1000
        db.session.commit()
    assert '-0.5<' in _history(c, ids['ours']) and '-0.2<' in _history(c, ids['ours'])
    assert 'Производство по поръчка' not in _history(c, ids['lot'])


def test_a_correction_returns_to_the_frozen_lot(c, ids):
    _post(c, 'item', ids['item'], 5)  # 0.5 sheet from our lot
    with flask_app.app_context():  # the detail moves to another material and gets bigger
        detail = db.session.get(Detail, ids['detail'])
        detail.material_key, detail.width = 'steel_c', 1000
        db.session.commit()
    _post(c, 'item', ids['item'], 2)  # gives back 3/5 of what was taken, to the lot it came from
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(10 - 0.2)
    assert _stock(MaterialPrice, ids['lot']) == pytest.approx(0.1)
    _post(c, 'item', ids['item'], 0)
    assert _stock(MaterialPrice, ids['ours']) == pytest.approx(10)
    with flask_app.app_context():
        assert db.session.get(OrderItem, ids['item']).material_consumed == 0
    assert 'Производство по поръчка' not in _history(c, ids['ours'])


def test_production_before_this_release_returns_nothing(c, ids):
    with flask_app.app_context():  # produced earlier, when production took no material
        db.session.get(OrderItem, ids['item']).quantity_produced = 5
        db.session.commit()
    _post(c, 'item', ids['item'], 0)
    assert _stock(MaterialPrice, ids['ours']) == 10


def test_own_material_line_takes_no_material(c, ids):
    with flask_app.app_context():
        own_item = db.session.get(OrderItem, ids['item'])
        own_item.own_material = True
        db.session.get(OrderItemComponent, ids['component']).order_item.own_material = True
        db.session.commit()
    assert _post(c, 'item', ids['item'], 5).get_json()['status'] == 'success'
    assert _post(c, 'component', ids['component'], 4).get_json()['status'] == 'success'
    assert _stock(MaterialPrice, ids['ours']) == 10
    assert _stock(Detail, ids['detail']) == 9  # the parts themselves still land in stock
    with flask_app.app_context():
        assert db.session.get(OrderItem, ids['item']).consumed_material_id is None