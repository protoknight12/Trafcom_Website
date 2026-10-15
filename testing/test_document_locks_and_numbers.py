"""
pytest: a document processed twice does not move stock twice (the route locks
the row and re-reads its state first), "max + 1" numbers are allocated under a
per-family lock, and deleting a Product / material / machine / user that
something still points at is refused instead of failing on the foreign key.

What SQLite cannot show: it has no FOR UPDATE and no advisory locks, and the
tests use one connection, so nothing here proves that a second PostgreSQL
transaction really waits. The PostgreSQL-only statements are checked as text
(compiled for that dialect / captured before they are sent); the logic around
them - the state is re-read after the lock, the loser takes the "already
done" branch - runs for real, with the stale row faked by changing it behind
the ORM's back. SQLite also does not enforce foreign keys, so the delete
tests prove the refusal, not the error it replaces.

Run with:
    pytest testing/test_document_locks_and_numbers.py -v
"""
import atexit
import os
import re
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from werkzeug.security import generate_password_hash

import app as app_module
from app import (app as flask_app, db, User, MaterialPrice, Detail, Product, Order, OrderItem, Offer, OfferItem,
                 ProductionOrder, Machine, BendProgram, GeneratorPreset, FlashingPreset, limiter,
                 seed_material_types, NUMBER_LOCK_KEYS, _lock_or_404, _lock_numbering, _next_erp_number,
                 _next_offer_number, _next_capa_number, _next_audit_number, _next_hall_no,
                 generate_order_number, _erp_number_conflict)


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана', cost_per_m2=20.0, cutting_speed_mm_per_min=2000,
                                     pierce_rate_per_min=30, type='sheets', stock_quantity=10))
        db.session.add(Detail(name='Планка', material_key='steel', width=100, height=50, total_length=300,
                              pierce_count=1, calculated_price=1.0, erp_number=555000))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _steel():
    return MaterialPrice.query.filter_by(key='steel').one()


def _plate():
    return Detail.query.filter_by(name='Планка').one()


def _stock():
    db.session.expire_all()
    return _steel().stock_quantity, _plate().stock_quantity


def _flashes(client):
    with client.session_transaction() as sess:
        return [message for _category, message in sess.get('_flashes', [])]


def _job():
    job = ProductionOrder(detail_id=_plate().id, material_id=_steel().id, quantity=4, planned_material_qty=0.02)
    db.session.add(job)
    db.session.commit()
    return job.id


def _order():
    order = Order(order_number='T-1', user_id=User.query.filter_by(username='qa_admin').one().id, customer_name='Клиент')
    db.session.add(order)
    db.session.flush()
    return order


# ----------------- Fix 1: the same document twice -----------------

def test_completing_a_done_job_again_moves_nothing(admin_client):
    job_id = _job()
    admin_client.post(f'/admin/production-orders/{job_id}/complete', data={'actual_material_qty': '2'})
    after_first = _stock()
    assert after_first == (8, 4)

    admin_client.post(f'/admin/production-orders/{job_id}/complete', data={'actual_material_qty': '2'})
    assert _stock() == after_first
    assert any('вече е завършена' in m for m in _flashes(admin_client))


def test_complete_rereads_the_job_after_locking_it(admin_client):
    """The race itself: this session still holds the job as 'pending' while
    another request has already completed it. The route must not trust its
    copy - _lock_or_404() reloads the row (populate_existing)."""
    job_id = _job()
    job = db.session.get(ProductionOrder, job_id)
    assert job.status == 'pending'  # loaded: the stale copy
    db.session.execute(text("UPDATE production_order SET status = 'done' WHERE id = :id"), {'id': job_id})

    admin_client.post(f'/admin/production-orders/{job_id}/complete', data={'actual_material_qty': '2'})
    assert _stock() == (10, 0), 'a job somebody else already completed must not move stock again'


def test_lock_or_404_refreshes_a_loaded_row_and_asks_for_update(admin_client):
    job_id = _job()
    job = db.session.get(ProductionOrder, job_id)
    db.session.execute(text('UPDATE production_order SET quantity = 99 WHERE id = :id'), {'id': job_id})

    seen = []
    session = db.session()
    listener = lambda state: seen.append(state.statement)
    event.listen(session, 'do_orm_execute', listener)
    try:
        assert _lock_or_404(ProductionOrder, job_id) is job
    finally:
        event.remove(session, 'do_orm_execute', listener)
    assert job.quantity == 99
    # SQLite drops FOR UPDATE; this is what PostgreSQL would be sent
    assert 'FOR UPDATE' in str(seen[0].compile(dialect=postgresql.dialect()))


def test_lock_or_404_is_a_404_for_a_missing_row(admin_client):
    assert admin_client.post('/admin/production-orders/424242/complete',
                             data={'actual_material_qty': '1'}).status_code == 404
    assert admin_client.post('/admin/delivery-notes/424242/delete').status_code == 404
    assert admin_client.post('/admin/client-delivery-notes/424242/delete').status_code == 404
    assert admin_client.post('/admin/production', data={'target_type': 'item', 'produced_qty': '1'}).status_code == 404


def test_the_same_production_report_twice_moves_stock_once(admin_client):
    order = _order()
    item = OrderItem(order_id=order.id, detail_id=_plate().id, quantity_ordered=5)
    db.session.add(item)
    db.session.commit()
    data = {'target_type': 'item', 'target_id': item.id, 'produced_qty': '3'}

    assert admin_client.post('/admin/production', data=data).get_json()['status'] == 'success'
    assert _stock()[1] == 3
    assert admin_client.post('/admin/production', data=data).get_json()['status'] == 'success'
    assert _stock()[1] == 3


def test_production_report_starts_from_the_committed_quantity(admin_client):
    """Two reports of "3 made" racing: the second one's session still shows 0
    produced, the row already says 3 - the delta must be 0, not another 3."""
    order = _order()
    item = OrderItem(order_id=order.id, detail_id=_plate().id, quantity_ordered=5)
    db.session.add(item)
    db.session.commit()
    assert db.session.get(OrderItem, item.id).quantity_produced == 0  # loaded: the stale copy
    db.session.execute(text('UPDATE order_item SET quantity_produced = 3 WHERE id = :id'), {'id': item.id})

    admin_client.post('/admin/production', data={'target_type': 'item', 'target_id': item.id, 'produced_qty': '3'})
    assert _stock()[1] == 0


# ----------------- Fix 2: numbering -----------------

def test_lock_numbering_does_nothing_on_sqlite(admin_client):
    sent = []
    listener = lambda conn, cursor, statement, *rest: sent.append(statement)
    event.listen(db.engine, 'before_cursor_execute', listener)
    try:
        for family in NUMBER_LOCK_KEYS:
            _lock_numbering(family)
    finally:
        event.remove(db.engine, 'before_cursor_execute', listener)
    assert sent == []


def test_lock_numbering_takes_a_transaction_advisory_lock_on_postgresql(admin_client, monkeypatch):
    """Not run against PostgreSQL - only that the right statement and key
    would be sent there."""
    sent = []
    monkeypatch.setattr(db.engine.dialect, 'name', 'postgresql')
    monkeypatch.setattr(db.session, 'execute', lambda statement, params=None: sent.append((str(statement), params)))
    _lock_numbering('erp')
    _lock_numbering('order')
    assert sent == [('SELECT pg_advisory_xact_lock(:key)', {'key': NUMBER_LOCK_KEYS['erp']}),
                    ('SELECT pg_advisory_xact_lock(:key)', {'key': NUMBER_LOCK_KEYS['order']})]


def test_lock_keys_are_distinct(admin_client):
    keys = list(NUMBER_LOCK_KEYS.values())
    assert len(set(keys)) == len(keys)
    assert 735100 not in keys  # _detection_lock()'s session-level advisory lock


def test_every_generator_locks_its_family_and_still_counts(admin_client, monkeypatch):
    locked = []
    monkeypatch.setattr(app_module, '_lock_numbering', locked.append)

    assert _next_erp_number() == 555001  # one past the detail's 555000
    assert _next_offer_number() == '00000000200'
    db.session.add(Offer(number='00000000200'))
    db.session.flush()
    assert _next_offer_number() == '00000000201'
    assert _next_capa_number() == 'CAPA-0001'
    assert _next_audit_number() == 'AUD-0001'
    assert _next_hall_no() == 1
    assert re.fullmatch(r'ORD-\d{4}-\d{4}', generate_order_number())
    assert locked == ['erp', 'offer', 'offer', 'capa', 'audit', 'hall', 'order']

    del locked[:]
    assert _erp_number_conflict(None) is None and locked == []  # nothing to check, nothing to lock
    assert 'Планка' in _erp_number_conflict(555000)
    assert locked == ['erp']


def test_typed_erp_number_of_another_table_is_refused(admin_client):
    res = admin_client.post('/api/quick-create-material', data={
        'display_name': 'QA Прът', 'type': 'rods', 'erp_number': '555000'})
    assert res.status_code == 400
    assert 'вече се използва' in res.get_json()['message']
    assert MaterialPrice.query.filter_by(display_name='QA Прът').first() is None

    product = Product(name='Шкаф', markup_percent=0.0, erp_number=555001)
    db.session.add(product)
    db.session.commit()
    res = admin_client.post('/api/quick-create-material', data={
        'display_name': 'QA Прът', 'type': 'rods', 'erp_number': '555001'})
    assert res.status_code == 400


# ----------------- Fix 3: deletes that something still points at -----------------

def _product(name='Шкаф'):
    product = Product(name=name, markup_percent=0.0)
    db.session.add(product)
    db.session.flush()
    return product


def test_product_on_an_order_line_is_not_deleted(admin_client):
    product = _product()
    db.session.add(OrderItem(order_id=_order().id, product_id=product.id, quantity_ordered=1))
    db.session.commit()

    res = admin_client.post(f'/admin/products/{product.id}/delete')
    assert res.status_code == 302
    assert any('поръчка' in m and 'не може да бъде изтрит' in m for m in _flashes(admin_client))
    assert db.session.get(Product, product.id) is not None


def test_product_on_an_offer_line_only_is_not_deleted(admin_client):
    product = _product()
    offer = Offer(number='00000000001')
    db.session.add(offer)
    db.session.flush()
    db.session.add(OfferItem(offer_id=offer.id, item_type='product', product_id=product.id, name='Шкаф'))
    db.session.commit()

    admin_client.post(f'/admin/products/{product.id}/delete')
    flashes = _flashes(admin_client)
    assert any('оферта' in m and 'не може да бъде изтрит' in m for m in flashes)
    assert not any('поръчка' in m for m in flashes)
    assert db.session.get(Product, product.id) is not None


def test_unreferenced_product_is_deleted(admin_client):
    product_id = _product().id
    db.session.commit()

    admin_client.post(f'/admin/products/{product_id}/delete')
    db.session.expire_all()
    assert db.session.get(Product, product_id) is None


def test_material_with_a_production_job_is_not_deleted(admin_client):
    spare = MaterialPrice(key='spare', display_name='Резервен', cost_per_m2=1.0, type='sheets')
    db.session.add(spare)
    db.session.flush()
    db.session.add(ProductionOrder(detail_id=_plate().id, material_id=spare.id, quantity=1, planned_material_qty=0.01))
    db.session.commit()

    admin_client.post('/admin/materials/spare/delete')
    assert any('задача за производство' in m and 'не може да бъде изтрит' in m for m in _flashes(admin_client))
    assert MaterialPrice.query.filter_by(key='spare').first() is not None


def test_machine_with_a_bend_program_is_not_deleted(admin_client):
    machine = Machine(name='Абкант')
    db.session.add(machine)
    db.session.flush()
    db.session.add(BendProgram(detail_id=_plate().id, machine_id=machine.id, name='Планка 90'))
    db.session.commit()

    admin_client.post(f'/machines/{machine.id}/delete')
    assert any('програми за огъване' in m and 'не може да бъде изтрита' in m for m in _flashes(admin_client))
    assert db.session.get(Machine, machine.id) is not None


def test_user_with_an_order_is_not_deleted(admin_client):
    user = User(username='qa_customer', password=generate_password_hash('irrelevant123'), role='regular_user')
    db.session.add(user)
    db.session.flush()
    db.session.add(Order(order_number='T-2', user_id=user.id, customer_name='Клиент'))
    db.session.commit()

    admin_client.post(f'/admin/delete_user/{user.id}')
    assert any('поръчки' in m and 'не може да бъде изтрит' in m for m in _flashes(admin_client))
    assert db.session.get(User, user.id) is not None


def test_user_with_only_presets_is_deleted_with_them(admin_client):
    user = User(username='qa_customer', password=generate_password_hash('irrelevant123'), role='regular_user')
    db.session.add(user)
    db.session.flush()
    user_id = user.id
    db.session.add(GeneratorPreset(user_id=user_id, name='Мой', settings_json='{}'))
    db.session.add(FlashingPreset(user_id=user_id, name='Мой', settings_json='{}'))
    db.session.commit()

    admin_client.post(f'/admin/delete_user/{user_id}')
    db.session.expire_all()
    assert db.session.get(User, user_id) is None, _flashes(admin_client)
    assert GeneratorPreset.query.count() == 0 and FlashingPreset.query.count() == 0
