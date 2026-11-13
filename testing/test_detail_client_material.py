"""
pytest: a private Detail may be cut from its owner's own stored material lot -
the lot costs nothing, the detail is only its Operations x the owner's own-material
surcharge (same number as the order-time "материал на клиента" checkbox), and a
lot of another client / a public detail + client lot is refused.

Run with:
    pytest testing/test_detail_client_material.py -v
"""
import atexit
import io
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

import ezdxf
import pytest
from flask import g
from flask_login import login_user
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Operation, Service, limiter,
                 _material_cost, detail_price_for, most_expensive_items)


def _square_dxf():
    doc = ezdxf.new()
    msp = doc.modelspace()
    pts = [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)]
    for a, b in zip(pts, pts[1:]):
        msp.add_line(a, b)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


@pytest.fixture
def setup():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        steel = MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100)
        a = Client(name='Клиент А', own_material_surcharge_percent=15)
        b = Client(name='Клиент Б')
        db.session.add_all([steel, a, b, Service(name='Фрезоване', price_per_hour_eur=60)])
        db.session.flush()
        lot_a = MaterialPrice(key='steel-a', display_name='Стомана', type='sheets', cost_per_m2=100, owner_client_id=a.id)
        lot_b = MaterialPrice(key='steel-b', display_name='Стомана', type='sheets', cost_per_m2=100, owner_client_id=b.id)
        db.session.add_all([lot_a, lot_b, User(username='adm', password=generate_password_hash('irrelevant123'), role='admin')])
        db.session.commit()
        yield dict(a=a, b=b, steel=steel, lot_a=lot_a, lot_b=lot_b, service=Service.query.one())
        db.session.remove()
        db.drop_all()


def _admin():
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'adm', 'password': 'irrelevant123'})
    return c


def _add(adm, name, owner, material):
    return adm.post('/admin/details/add', data={'name': name, 'material': material, 'owner_client_id': str(owner),
                                                'file': (io.BytesIO(_square_dxf()), 'sq.dxf')},
                    content_type='multipart/form-data')


def test_material_cost_of_client_lot_is_zero(setup):
    assert _material_cost(1000, 1000, setup['lot_a']) == 0.0
    assert _material_cost(1000, 1000, setup['steel']) == 100


def test_detail_on_owner_lot_prices_operations_only(setup):
    s = setup
    adm = _admin()
    assert _add(adm, 'Мой', s['a'].id, 'steel-a').status_code == 302
    d = Detail.query.filter_by(name='Мой').one()
    assert d.calculated_price == 0 and d.client_material
    assert d.material_price_breakdown.startswith('материал на клиента')
    assert '+15% върху операциите' in d.material_price_breakdown
    db.session.add(Operation(detail_id=d.id, service_id=s['service'].id, duration_minutes=60))
    db.session.commit()
    db.session.refresh(d)
    assert d.total_price == 69.0
    assert detail_price_for(d, s['a'], own_material=True) == 69.0   # no double surcharge
    assert detail_price_for(d, s['a']) == 69.0


def test_foreign_or_public_use_of_client_lot_refused(setup):
    s = setup
    adm = _admin()
    for name, owner, mat in (('Чужд', s['a'].id, 'steel-b'), ('Публичен', '', 'steel-a')):
        r = _add(adm, name, owner, mat)
        assert r.status_code == 302
        assert Detail.query.filter_by(name=name).first() is None
        with adm.session_transaction() as sess:
            flashes = [m for _, m in sess.get('_flashes', [])]
        assert 'Невалиден избор на материал.' in flashes


def test_owner_change_blocked_until_material_changed(setup):
    s = setup
    adm = _admin()
    _add(adm, 'Мой', s['a'].id, 'steel-a')
    d = Detail.query.filter_by(name='Мой').one()
    adm.post(f'/admin/details/{d.id}/owner', data={'owner_client_id': str(s['b'].id)})
    db.session.expire_all()
    assert db.session.get(Detail, d.id).owner_client_id == s['a'].id
    r = adm.post(f'/details/{d.id}/update-material', data={'material': 'steel', 'width': '1000', 'height': '1000'})
    assert r.status_code == 302
    db.session.expire_all()
    d = db.session.get(Detail, d.id)
    assert d.material_key == 'steel' and d.calculated_price == 100
    adm.post(f'/admin/details/{d.id}/owner', data={'owner_client_id': str(s['b'].id)})
    db.session.expire_all()
    assert db.session.get(Detail, d.id).owner_client_id == s['b'].id


def test_pickers_offer_only_matching_lots(setup):
    s = setup
    adm = _admin()
    _add(adm, 'Мой', s['a'].id, 'steel-a')
    d = Detail.query.filter_by(name='Мой').one()
    html = adm.get(f'/details/{d.id}/files').get_data(as_text=True)
    assert 'value="steel-a"' in html and 'value="steel-b"' not in html
    html = adm.get('/admin/details').get_data(as_text=True)
    assert f'value="steel-a" data-owner="{s["a"].id}"' in html


def _flashes(c):
    with c.session_transaction() as sess:
        return [m for _, m in sess.get('_flashes', [])]


def _add_a_with_hour_operation(adm, s):
    _add(adm, 'Мой', s['a'].id, 'steel-a')
    d = Detail.query.filter_by(name='Мой').one()
    db.session.add(Operation(detail_id=d.id, service_id=s['service'].id, duration_minutes=60))
    db.session.commit()
    return d


def test_manual_price_on_client_lot_refused(setup):
    s = setup
    adm = _admin()
    r = adm.post('/admin/details/add', data={'name': 'Ръчен', 'material': 'steel-a', 'owner_client_id': str(s['a'].id),
                                              'manual_price': '50'})
    assert r.status_code == 302
    assert Detail.query.filter_by(name='Ръчен').first() is None
    assert 'Детайл от материал на клиента няма ръчна цена - качете DXF или добавете операции.' in _flashes(adm)


def test_production_wizard_hides_client_material_details(setup):
    s = setup
    adm = _admin()
    _add(adm, 'Клиентски детайл', s['a'].id, 'steel-a')
    _add(adm, 'Обикновен детайл', '', 'steel')
    adm.get('/admin/production-orders')   # the first page view eats the "added" flash messages, which carry the names
    html = adm.get('/admin/production-orders').get_data(as_text=True)
    assert 'Обикновен детайл' in html and 'Клиентски детайл' not in html


def test_chatbot_most_expensive_uses_total_price(setup):
    s = setup
    adm = _admin()
    _add_a_with_hour_operation(adm, s)
    with flask_app.test_request_context():
        login_user(User.query.filter_by(username='adm').first())
        out = most_expensive_items.func()
    assert 'Детайл: Мой - 69 €' in out


def test_dashboard_shows_surcharge_and_factor(setup):
    s = setup
    adm = _admin()
    d = _add_a_with_hour_operation(adm, s)
    html = adm.get(f'/details/{d.id}/files').get_data(as_text=True)
    assert 'const OWN_FACTOR = 1.15' in html
    assert 'Надценка „материал на клиента“ (+15%)' in html
    assert '9.00 €' in html
