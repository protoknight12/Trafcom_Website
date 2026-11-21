"""
pytest: "Протокол за прехвърляне на материал" (MaterialTransfer) - stored material changes owner: ours -> a client,
a client -> us, a client -> another client. Target lot at price 0 (reused next time), stock moved, refusals move
nothing, delete reverses (refused once the target was used), history sums to stock, print page, delete refusals.

Run with:
    pytest testing/test_material_transfer.py -v
"""
import json

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, MaterialTransfer, MaterialTransferItem, limiter,
                 seed_material_types, _bump_stock)


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker'))
        db.session.add(Client(name='Клиент А'))
        db.session.add(Client(name='Клиент Б'))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=25.0, price_per_unit=50.0,
                                     cutting_speed_mm_per_min=2000, pierce_rate_per_min=30, type='sheets',
                                     thickness_mm=2.0, sheet_width_mm=1000, sheet_length_mm=2000, brand='DC01',
                                     weight_kg=31.4, stock_quantity=10, erp_number=1))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _ids():
    a = Client.query.filter_by(name='Клиент А').one().id
    b = Client.query.filter_by(name='Клиент Б').one().id
    return a, b


def _post(c, from_owner, to_owner, lines):
    return c.post('/admin/material-transfers/create', data={
        'from_owner': '' if from_owner is None else str(from_owner),
        'to_owner': '' if to_owner is None else str(to_owner),
        'items_json': json.dumps([{'material_id': m, 'qty': q} for m, q in lines])})


def _stock(mid):
    db.session.expire_all()
    return db.session.get(MaterialPrice, mid).stock_quantity


def _steel():
    return MaterialPrice.query.filter_by(key='steel').one()


def test_ours_to_client_creates_zero_price_lot_and_reuses_it(admin_client):
    with flask_app.app_context():
        a, _ = _ids()
        steel = _steel()
        r = _post(admin_client, None, a, [(steel.id, 3)])
        assert r.status_code == 302 and '/admin/material-transfers/' in r.location
        lot = MaterialPrice.query.filter_by(owner_client_id=a).one()
        assert lot.cost_per_m2 == 0 and lot.price_per_unit is None and lot.stock_quantity == 3
        assert (lot.display_name, lot.brand, lot.sheet_width_mm, lot.thickness_mm, lot.weight_kg,
                lot.cutting_speed_mm_per_min) == ('Стомана 2мм', 'DC01', 1000, 2.0, 31.4, 2000)
        assert lot.erp_number and lot.erp_number != steel.erp_number and lot.key == f'material_{lot.id}'
        assert _stock(steel.id) == 7
        t = MaterialTransfer.query.one()
        assert t.from_client_id is None and t.to_client_id == a and t.doc_number == '30000001'
        _post(admin_client, None, a, [(steel.id, 2)])
        assert MaterialPrice.query.filter_by(owner_client_id=a).count() == 1     # same lot again
        assert _stock(lot.id) == 5 and _stock(steel.id) == 5
        assert MaterialTransfer.query.order_by(MaterialTransfer.number).all()[-1].number == 2


def test_client_to_us_and_client_to_client(admin_client):
    with flask_app.app_context():
        a, b = _ids()
        _post(admin_client, None, a, [(_steel().id, 4)])
        lot_a = MaterialPrice.query.filter_by(owner_client_id=a).one()
        _post(admin_client, a, None, [(lot_a.id, 1)])
        ours_zero = MaterialPrice.query.filter(MaterialPrice.owner_client_id.is_(None),
                                               MaterialPrice.cost_per_m2 == 0).one()
        assert ours_zero.id != _steel().id and ours_zero.stock_quantity == 1   # never merged into our priced lot
        _post(admin_client, a, b, [(lot_a.id, 2)])
        lot_b = MaterialPrice.query.filter_by(owner_client_id=b).one()
        assert lot_b.stock_quantity == 2 and lot_b.cost_per_m2 == 0 and _stock(lot_a.id) == 1


@pytest.mark.parametrize('case', ['over', 'zero', 'wrong_owner', 'same_owner', 'empty', 'bad_client'])
def test_refusals_move_nothing(admin_client, case):
    with flask_app.app_context():
        a, b = _ids()
        steel = _steel()
        args = {'over': (None, a, [(steel.id, 11)]), 'zero': (None, a, [(steel.id, 0)]),
                'wrong_owner': (a, b, [(steel.id, 1)]), 'same_owner': (a, a, [(steel.id, 1)]),
                'empty': (None, a, []), 'bad_client': (None, 9999, [(steel.id, 1)])}[case]
        r = _post(admin_client, *args)
        assert r.status_code == 302 and r.location.endswith('/admin/material-transfers')
        assert MaterialTransfer.query.count() == 0 and MaterialTransferItem.query.count() == 0
        assert _stock(steel.id) == 10 and MaterialPrice.query.count() == 1


def test_worker_refused(admin_client):
    with flask_app.app_context():
        a, _ = _ids()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
        _post(c, None, a, [(_steel().id, 1)])
        assert c.get('/admin/material-transfers').status_code == 302
        assert MaterialTransfer.query.count() == 0 and _stock(_steel().id) == 10


def test_delete_reverses_and_refuses_when_target_used(admin_client):
    with flask_app.app_context():
        a, _ = _ids()
        steel = _steel()
        _post(admin_client, None, a, [(steel.id, 3)])
        t = MaterialTransfer.query.one()
        lot = MaterialPrice.query.filter_by(owner_client_id=a).one()
        _bump_stock(lot, -1)            # the client's lot was used
        db.session.commit()
        admin_client.post(f'/admin/material-transfers/{t.id}/delete')
        assert MaterialTransfer.query.count() == 1 and _stock(steel.id) == 7 and _stock(lot.id) == 2
        _bump_stock(db.session.get(MaterialPrice, lot.id), 1)
        db.session.commit()
        admin_client.post(f'/admin/material-transfers/{t.id}/delete')
        assert MaterialTransfer.query.count() == 0 and MaterialTransferItem.query.count() == 0
        assert _stock(steel.id) == 10 and _stock(lot.id) == 0


def test_history_and_print(admin_client):
    with flask_app.app_context():
        a, _ = _ids()
        steel = _steel()
        _post(admin_client, None, a, [(steel.id, 3)])
        lot = MaterialPrice.query.filter_by(owner_client_id=a).one()
        out = admin_client.get(f'/admin/materials/{steel.id}/history').get_data(as_text=True)
        assert 'Прехвърлен към Клиент А' in out and '30000001' in out
        inn = admin_client.get(f'/admin/materials/{lot.id}/history').get_data(as_text=True)
        assert 'Прехвърлен от Трафком' in inn and '+3 листа = 6 м²' in inn
        t = MaterialTransfer.query.one()
        page = admin_client.get(f'/admin/material-transfers/{t.id}').get_data(as_text=True)
        assert 'Протокол за прехвърляне на материал' in page and '30000001' in page
        assert '3 листа = 6 м²' in page and 'Клиент А' in page
        assert 'Клиент А' in admin_client.get('/admin/material-transfers').get_data(as_text=True)
        assert 'Прехвърли' in admin_client.get('/storage/materials').get_data(as_text=True)


def test_delete_material_and_client_refused(admin_client):
    with flask_app.app_context():
        a, _ = _ids()
        _post(admin_client, None, a, [(_steel().id, 3)])
        lot = MaterialPrice.query.filter_by(owner_client_id=a).one()
        _bump_stock(lot, -3)
        db.session.commit()
        r = admin_client.post(f'/admin/materials/{lot.key}/delete', follow_redirects=True)
        assert 'протокол за прехвърляне' in r.get_data(as_text=True)
        assert db.session.get(MaterialPrice, lot.id) is not None
        r = admin_client.post(f'/admin/clients/{a}/delete', follow_redirects=True)
        assert 'протоколи за прехвърляне на материал' in r.get_data(as_text=True)
        assert db.session.get(Client, a) is not None


def test_history_sums_to_stock(admin_client):
    with flask_app.app_context():
        a, b = _ids()
        steel = _steel()
        _post(admin_client, None, a, [(steel.id, 4)])
        lot_a = MaterialPrice.query.filter_by(owner_client_id=a).one()
        _post(admin_client, a, b, [(lot_a.id, 1.5)])
        _post(admin_client, None, a, [(steel.id, 1)])
        captured = {}
        import flask
        def grab(sender, template, context, **extra):
            captured['movements'] = context['movements']
        flask.template_rendered.connect(grab, flask_app)
        try:
            for mid, start in ((lot_a.id, 0), (MaterialPrice.query.filter_by(owner_client_id=b).one().id, 0)):
                admin_client.get(f'/admin/materials/{mid}/history')
                net = sum(m['quantity'] * (1 if m['direction'] == 'in' else -1) for m in captured['movements'])
                assert abs(start + net - _stock(mid)) < 1e-9
            admin_client.get(f'/admin/materials/{steel.id}/history')
            net = sum(m['quantity'] * (1 if m['direction'] == 'in' else -1) for m in captured['movements'])
            assert abs(10 + net - _stock(steel.id)) < 1e-9 and _stock(steel.id) == 5
        finally:
            flask.template_rendered.disconnect(grab, flask_app)
