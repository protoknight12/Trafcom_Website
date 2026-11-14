"""
pytest: admin_order_issue() - "Изпиши към клиента" on a completed order. One 'return' ClientDeliveryNote
with a line per produced piece (standalone Detail / component-less Product at the line's frozen price,
a product tracked per component as its component Details), stock goes down through the manual note's
own code, Order.issued_note_id makes it once-only, deleting the note frees the order, deleting the order
does not break on the new FK.

Run with:
    pytest testing/test_order_quick_issue.py -v
"""
import atexit
import json
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

from datetime import datetime

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, Detail, Product, MaterialPrice, Order, OrderItem, OrderItemComponent,
                 ClientDeliveryNote, ClientDeliveryNoteItem, APP_TZ, limiter, seed_material_types)


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
        db.session.add_all([admin, User(username='qa_worker', password=pw, role='worker'), client])
        db.session.flush()
        db.session.add(User(username='qa_customer', password=pw, role='regular_user', client_id=client.id))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана', cost_per_m2=10, type='sheets',
                                     cutting_speed_mm_per_min=1000, pierce_rate_per_min=10))
        plain = Detail(name='Планка', material_key='steel', width=100, height=100, total_length=1, pierce_count=1, calculated_price=5.0, stock_quantity=5)
        part = Detail(name='Ъгълник', material_key='steel', width=100, height=100, total_length=1, pierce_count=1, calculated_price=2.0, stock_quantity=4)
        private = Detail(name='Планка клиент', material_key='steel', width=100, height=100, total_length=1, pierce_count=1, calculated_price=1.0, stock_quantity=1,
                         owner_client_id=client.id)
        loose = Product(name='Кутия', markup_percent=0, stock_quantity=3)
        assembly = Product(name='Изделие', markup_percent=0, stock_quantity=0)
        db.session.add_all([plain, part, private, loose, assembly])
        db.session.commit()
        yield dict(client=client.id, plain=plain.id, part=part.id, private=private.id, loose=loose.id,
                   assembly=assembly.id, admin=admin.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def _order(ids, lines, status='completed', client=True, number='ORD-1', **extra):
    """lines: (kind, id, ordered, produced, price[, component produced])"""
    with flask_app.app_context():
        order = Order(order_number=number, user_id=ids['admin'], customer_name='QA', status=status,
                      client_id=ids['client'] if client else None, **extra)
        db.session.add(order)
        db.session.flush()
        for kind, row_id, ordered, produced, price, *comp in lines:
            item = OrderItem(order_id=order.id, quantity_ordered=ordered, quantity_produced=produced,
                             unit_price=price, **{f'{kind}_id': row_id})
            db.session.add(item)
            db.session.flush()
            if comp:
                db.session.add(OrderItemComponent(order_item_id=item.id, detail_id=ids['part'],
                                                  detail_name_snapshot='Ъгълник', quantity_needed=ordered * 2,
                                                  quantity_produced=comp[0]))
        db.session.commit()
        return order.id


def _full_order(ids, **kw):
    return _order(ids, [('detail', ids['plain'], 3, 3, 4.5), ('product', ids['loose'], 2, 2, 9.0),
                        ('product', ids['assembly'], 2, 0, 20.0, 3)], **kw)


def _issue(c, order_id):
    return c.post(f'/admin/orders/{order_id}/issue')


def _stock(model, row_id):
    with flask_app.app_context():
        return db.session.get(model, row_id).stock_quantity


def _notes():
    with flask_app.app_context():
        return ClientDeliveryNote.query.all()


def _issued_note_id(order_id):
    with flask_app.app_context():
        return db.session.get(Order, order_id).issued_note_id


def test_issue_creates_one_note_and_lowers_stock(ids):
    order_id = _full_order(ids)
    res = _issue(_login('qa_admin'), order_id)
    assert res.status_code == 302
    with flask_app.app_context():
        (note,) = ClientDeliveryNote.query.all()
        assert res.headers['Location'].endswith(f'/admin/client-delivery-notes/{note.id}/print')
        assert (note.kind, note.client_id, note.note_number, note.note_date) == (
            'return', ids['client'], 'ORD-1', datetime.now(APP_TZ).date())
        lines = {(i.target_type, i.target_id): (i.quantity, i.unit_price, i.description_snapshot) for i in note.items}
        assert lines == {('detail', ids['plain']): (3, 4.5, 'Планка'),
                         ('product', ids['loose']): (2, 9.0, 'Кутия'),
                         ('detail', ids['part']): (3, None, 'Ъгълник')}   # the assembly goes out as its parts
        assert db.session.get(Order, order_id).issued_note_id == note.id
    assert _stock(Detail, ids['plain']) == 2 and _stock(Product, ids['loose']) == 1
    assert _stock(Detail, ids['part']) == 1 and _stock(Product, ids['assembly']) == 0


def test_second_issue_is_refused(ids):
    order_id = _full_order(ids)
    c = _login('qa_admin')
    _issue(c, order_id)
    res = _issue(c, order_id)
    assert res.status_code == 302 and '/print' not in res.headers['Location']
    assert 'вече е изписана' in c.get('/admin/production').get_data(as_text=True)
    assert len(_notes()) == 1
    assert _stock(Detail, ids['plain']) == 2   # not lowered twice


@pytest.mark.parametrize('kw', [dict(status='new'), dict(status='cancelled'), dict(client=False),
                                dict(deleted_at=datetime(2026, 1, 1))])
def test_not_issuable_orders_are_refused(ids, kw):
    order_id = _full_order(ids, **kw)
    _issue(_login('qa_admin'), order_id)
    assert not _notes()
    assert _stock(Detail, ids['plain']) == 5
    assert _issued_note_id(order_id) is None


def test_nothing_produced_is_refused(ids):
    order_id = _order(ids, [('detail', ids['plain'], 3, 0, 4.5)])
    c = _login('qa_admin')
    _issue(c, order_id)
    assert 'Няма изработени' in c.get('/admin/production').get_data(as_text=True)
    assert not _notes()
    assert _stock(Detail, ids['plain']) == 5


def test_zero_produced_line_is_skipped(ids):
    order_id = _order(ids, [('detail', ids['plain'], 3, 2, 4.5), ('product', ids['loose'], 2, 0, 9.0)])
    _issue(_login('qa_admin'), order_id)
    assert len(_notes()) == 1
    with flask_app.app_context():
        assert [(i.target_type, i.quantity) for i in ClientDeliveryNoteItem.query.all()] == [('detail', 2)]
    assert _stock(Product, ids['loose']) == 3


def test_clients_own_detail_may_not_go_negative(ids):
    order_id = _order(ids, [('detail', ids['private'], 3, 3, 1.0)])   # stock 1, 3 issued
    _issue(_login('qa_admin'), order_id)
    assert not _notes()
    assert _stock(Detail, ids['private']) == 1
    assert _issued_note_id(order_id) is None


def test_our_rows_may_go_negative(ids):
    order_id = _order(ids, [('detail', ids['plain'], 9, 9, 4.5)])   # stock 5, 9 issued
    _issue(_login('qa_admin'), order_id)
    assert len(_notes()) == 1 and _stock(Detail, ids['plain']) == -4


@pytest.mark.parametrize('username', ['qa_worker', 'qa_customer'])
def test_only_admin_may_issue(ids, username):
    order_id = _full_order(ids)
    res = _issue(_login(username), order_id)
    assert res.status_code == 302 and not _notes()
    assert _stock(Detail, ids['plain']) == 5


def test_deleting_the_note_frees_the_order(ids):
    order_id = _full_order(ids)
    c = _login('qa_admin')
    _issue(c, order_id)
    (note,) = _notes()
    assert c.post(f'/admin/client-delivery-notes/{note.id}/delete').status_code == 302
    assert not _notes()
    assert _stock(Detail, ids['plain']) == 5 and _stock(Detail, ids['part']) == 4   # given back
    assert _issued_note_id(order_id) is None
    _issue(c, order_id)
    assert len(_notes()) == 1


def test_editing_the_note_keeps_the_link(ids):
    order_id = _full_order(ids)
    c = _login('qa_admin')
    _issue(c, order_id)
    (note,) = _notes()
    c.post(f'/admin/client-delivery-notes/{note.id}/edit', data={
        'note_number': 'F-1', 'note_date': '2026-11-01',
        'items_json': json.dumps([{'type': 'detail', 'target_id': ids['plain'], 'qty': 1}])})
    with flask_app.app_context():
        order = db.session.get(Order, order_id)
        assert order.issued_note_id == note.id and order.issued_note.note_number == 'F-1'


def test_deleting_an_issued_order_does_not_break(ids):
    c = _login('qa_admin')
    produced = _full_order(ids)
    _issue(c, produced)                                   # soft delete (it moved stock)
    assert c.post(f'/admin/orders/{produced}/delete').status_code == 302
    with flask_app.app_context():
        order = db.session.get(Order, produced)
        assert order.deleted_at is not None and order.issued_note_id is None
        assert ClientDeliveryNote.query.count() == 1      # the note stays

    untouched = _order(ids, [('detail', ids['plain'], 1, 1, 4.5)], number='ORD-2')
    _issue(c, untouched)
    with flask_app.app_context():                         # nothing "produced" any more: hard delete
        OrderItem.query.filter_by(order_id=untouched).update({'quantity_produced': 0})
        db.session.commit()
    assert c.post(f'/admin/orders/{untouched}/delete').status_code == 302
    with flask_app.app_context():
        assert db.session.get(Order, untouched) is None
        assert ClientDeliveryNote.query.count() == 2


def test_report_shows_button_then_link(ids):
    order_id = _full_order(ids)
    c = _login('qa_admin')
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Изпиши към клиента' in page and f'/admin/orders/{order_id}/issue' in page
    _issue(c, order_id)
    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Изписана с разписка №ORD-1' in page and f'/admin/orders/{order_id}/issue' not in page
    other = _full_order(ids, status='in_production', number='ORD-3')
    assert f'/admin/orders/{other}/issue' not in c.get('/admin/production').get_data(as_text=True)
