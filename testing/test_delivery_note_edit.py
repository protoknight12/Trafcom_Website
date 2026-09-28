"""
pytest: editing/deleting a recorded delivery note (edit_/delete_delivery_note,
edit_/delete_client_delivery_note) - old stock movements reversed, new lines
applied; a kept material line with a corrected price re-prices its lot in
place unless another note uses that lot (then the normal price-lot split).

Run with:
    pytest testing/test_delivery_note_edit.py -v
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
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, DeliveryNote, ClientDeliveryNote,
                 limiter, seed_material_types)


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=0.0,
                                     cutting_speed_mm_per_min=2000, pierce_rate_per_min=30, type='sheets',
                                     thickness_mm=2.0, sheet_width_mm=1000, sheet_length_mm=2000))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _line(qty, price, item_id=None):
    return {'type': 'material', 'name': 'Стомана 2мм', 'material_key': 'steel', 'qty': qty, 'unit_price': price,
            'width': 1000, 'height': 2000, 'thickness': 2.0, 'material_type': 'sheets', 'item_id': item_id}


def _post(client, url, lines, **header):
    return client.post(url, data={'items_json': json.dumps(lines), **header})


def test_edit_reverses_stock_and_corrects_price_in_place(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_line(10, 40)], note_number='A1')
    note = DeliveryNote.query.one()
    steel = MaterialPrice.query.filter_by(key='steel').one()
    assert steel.stock_quantity == 10 and steel.cost_per_m2 == 20

    # typo fix: 4 sheets at 50 € (not 10 at 40) - same lot, re-priced, stock 4
    _post(admin_client, f'/admin/delivery-notes/{note.id}/edit', [_line(4, 50, note.items[0].id)], note_number='A2')
    db.session.expire_all()
    assert MaterialPrice.query.count() == 1
    steel = MaterialPrice.query.filter_by(key='steel').one()
    assert steel.stock_quantity == 4 and steel.cost_per_m2 == 25 and steel.price_per_unit == 50
    note = DeliveryNote.query.one()
    assert note.note_number == 'A2' and len(note.items) == 1 and note.items[0].quantity == 4


def test_price_change_on_shared_lot_splits(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_line(10, 40)])
    _post(admin_client, '/admin/delivery-notes/create', [_line(5, 40)])
    first = DeliveryNote.query.order_by(DeliveryNote.id).first()
    _post(admin_client, f'/admin/delivery-notes/{first.id}/edit', [_line(10, 60, first.items[0].id)])
    db.session.expire_all()
    lots = {m.cost_per_m2: m.stock_quantity for m in MaterialPrice.query.all()}
    assert lots == {20: 5, 30: 10}, lots  # the other note's lot keeps its price and stock


def test_client_note_edit_gives_stock_back(admin_client):
    steel = MaterialPrice.query.filter_by(key='steel').one()
    steel.stock_quantity = 10
    db.session.commit()
    row = {'type': 'material', 'target_id': steel.id, 'qty': 3, 'unit_price': None}
    _post(admin_client, '/admin/client-delivery-notes/create', [row])
    note = ClientDeliveryNote.query.one()
    db.session.expire_all()
    assert MaterialPrice.query.get(steel.id).stock_quantity == 7
    _post(admin_client, f'/admin/client-delivery-notes/{note.id}/edit', [dict(row, qty=1)])
    db.session.expire_all()
    assert MaterialPrice.query.get(steel.id).stock_quantity == 9
    assert [it.quantity for it in ClientDeliveryNote.query.one().items] == [1]


def test_delete_reverses_stock_both_ways(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_line(10, 40)])
    steel_id = MaterialPrice.query.filter_by(key='steel').one().id
    _post(admin_client, '/admin/client-delivery-notes/create', [{'type': 'material', 'target_id': steel_id, 'qty': 3}])
    db.session.expire_all()
    assert MaterialPrice.query.get(steel_id).stock_quantity == 7

    admin_client.post(f'/admin/client-delivery-notes/{ClientDeliveryNote.query.one().id}/delete')
    db.session.expire_all()
    assert MaterialPrice.query.get(steel_id).stock_quantity == 10 and ClientDeliveryNote.query.count() == 0

    admin_client.post(f'/admin/delivery-notes/{DeliveryNote.query.one().id}/delete')
    db.session.expire_all()
    steel = MaterialPrice.query.get(steel_id)
    assert steel.stock_quantity == 0 and DeliveryNote.query.count() == 0
    assert steel.cost_per_m2 == 20  # the catalog row itself stays


def test_empty_edit_leaves_note_untouched(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_line(10, 40)])
    note = DeliveryNote.query.one()
    _post(admin_client, f'/admin/delivery-notes/{note.id}/edit', [{'type': 'material', 'name': '', 'qty': 1}])
    db.session.expire_all()
    assert MaterialPrice.query.filter_by(key='steel').one().stock_quantity == 10
    assert len(DeliveryNote.query.one().items) == 1
