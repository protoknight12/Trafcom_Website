"""
pytest: admin_client_delivery_note_breakdown() / _note_breakdown() - an issue note printed as its materials and its
operations, the same ones summed, each line's price split between its parts so the rows add up to the note's total.
A note issued from an order takes the order's chosen material, the client's-material flag and the order-time
operations. A consume note has no such print.

Run with:
    pytest testing/test_note_breakdown.py -v
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
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, Detail, Product, ProductDetail, ProductExtraCost, MaterialPrice,
                 Service, Operation, Order, OrderItem, OrderItemOperation, ClientDeliveryNote, ClientDeliveryNoteItem,
                 limiter, seed_material_types, _note_breakdown)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        client = Client(name='Клиент А')
        admin = User(username='qa_admin', password=pw, role='admin')
        db.session.add_all([admin, User(username='qa_worker', password=pw, role='worker'), client,
                            MaterialPrice(key='steel', display_name='Стомана', cost_per_m2=10, type='sheets', thickness_mm=2)])
        bend = Service(name='Огъване', price_per_hour_eur=60)
        weld = Service(name='Заваряване', price_per_hour_eur=30)
        db.session.add_all([bend, weld])
        db.session.flush()
        a = Detail(name='Планка', material_key='steel', width=100, height=100, total_length=1, pierce_count=1, calculated_price=0.1)
        b = Detail(name='Скоба', material_key='steel', width=200, height=100, total_length=1, pierce_count=1, calculated_price=0.2)
        box = Product(name='Кутия', markup_percent=0)
        db.session.add_all([a, b, box])
        db.session.flush()
        db.session.add_all([Operation(detail_id=a.id, service_id=bend.id, duration_minutes=6),     # 6.00 €
                            Operation(detail_id=b.id, service_id=bend.id, duration_minutes=3),     # 3.00 €
                            ProductDetail(product_id=box.id, detail_id=a.id, quantity=2),
                            ProductExtraCost(product_id=box.id, label='Боядисване', amount=5)])
        db.session.commit()
        yield dict(client=client.id, admin=admin.id, a=a.id, b=b.id, box=box.id, weld=weld.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def _note(ids, lines, kind='issue'):
    with flask_app.app_context():
        note = ClientDeliveryNote(client_id=ids['client'], kind=kind, created_by_id=ids['admin'])
        db.session.add(note)
        db.session.flush()
        for t, t_id, qty, price in lines:
            db.session.add(ClientDeliveryNoteItem(client_delivery_note_id=note.id, target_type=t, target_id=t_id,
                                                  description_snapshot='x', quantity=qty, unit_price=price))
        db.session.commit()
        return note.id


def _by_name(rows):
    return {r['name']: r for r in rows}


def test_same_materials_and_operations_are_summed_and_add_up_to_the_note(ids):
    note_id = _note(ids, [('detail', ids['a'], 10, 7.0), ('detail', ids['b'], 5, 4.0), ('product', ids['box'], 1, 20.0)])
    with flask_app.app_context():
        bd = _note_breakdown(db.session.get(ClientDeliveryNote, note_id))
    mats, ops = _by_name(bd['materials']), _by_name(bd['operations'])
    assert list(mats) == ['Стомана, 2 мм']
    assert mats['Стомана, 2 мм']['qty'] == pytest.approx(10 * 0.01 + 5 * 0.02 + 2 * 0.01)   # m²
    assert mats['Стомана, 2 мм']['unit'] == 'м²'
    assert set(ops) == {'Огъване', 'Боядисване'}
    assert ops['Огъване']['qty'] == pytest.approx((10 * 6 + 5 * 3 + 2 * 6) / 60)            # hours
    assert ops['Огъване']['unit'] == 'ч'
    # the price of a line is split by list cost: Планка 70 € = material 1 : bending 60
    assert bd['total'] == 110.0
    assert round(bd['materials_total'] + bd['operations_total'], 2) == 110.0
    assert sum(r['value'] for r in bd['materials'] + bd['operations']) == pytest.approx(110.0)
    assert ops['Огъване']['value'] > mats['Стомана, 2 мм']['value']


def test_note_from_an_order_uses_its_material_flag_and_order_operations(ids):
    with flask_app.app_context():
        order = Order(order_number='ORD-9', user_id=ids['admin'], customer_name='QA', status='completed', client_id=ids['client'])
        db.session.add(order)
        db.session.flush()
        own = OrderItem(order_id=order.id, detail_id=ids['a'], quantity_ordered=4, quantity_produced=4, unit_price=9.0, own_material=True)
        db.session.add(own)
        db.session.flush()
        db.session.add(OrderItemOperation(order_item_id=own.id, service_id=ids['weld'], duration_minutes=4))   # 2.00 €
        db.session.commit()
        order_id = order.id
    c = _login('qa_admin')
    resp = c.post(f'/admin/orders/{order_id}/issue')
    assert resp.status_code == 302
    with flask_app.app_context():
        note = db.session.get(Order, order_id).issued_note_id
        bd = _note_breakdown(db.session.get(ClientDeliveryNote, note))
    mats, ops = _by_name(bd['materials']), _by_name(bd['operations'])
    assert mats['Стомана, 2 мм (материал на клиента)']['value'] == 0          # the client's material has no value
    assert ops['Заваряване']['qty'] == pytest.approx(4 * 4 / 60)
    assert bd['total'] == 36.0 and sum(r['value'] for r in bd['materials'] + bd['operations']) == pytest.approx(36.0)


def test_print_page_button_worker_access_and_consume_refused(ids):
    note_id = _note(ids, [('detail', ids['a'], 2, 7.0)])
    consume_id = _note(ids, [('detail', ids['a'], 1, None)], kind='consume')
    c = _login('qa_worker')
    page = c.get(f'/admin/client-delivery-notes/{note_id}/breakdown')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'Спецификация на материали и операции' in html and 'Огъване' in html and 'Общо с ДДС' in html
    assert f'/admin/client-delivery-notes/{note_id}/breakdown' in c.get(f'/admin/client-delivery-notes/{note_id}/print').get_data(as_text=True)
    assert c.get(f'/admin/client-delivery-notes/{consume_id}/breakdown').status_code == 404
    assert f'/{consume_id}/breakdown' not in c.get(f'/admin/client-delivery-notes/{consume_id}/print').get_data(as_text=True)
