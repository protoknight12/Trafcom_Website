"""
pytest: per-client access limits (Client.access_json) - the admin page saves
"Всички"/"Само избраните" per section, and a linked regular user is then
blocked from a disallowed app and only sees allowed machines/services/public
details/products (own private items always stay visible). Staff unaffected.

Run with:
    pytest testing/test_client_access.py -v
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
atexit.register(lambda: os.path.exists(_db_path) and _safe_remove())


def _safe_remove():
    try:
        os.remove(_db_path)
    except OSError:
        pass


os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Machine, Service, Detail, Product, limiter,
                 client_access, catalog_item_allowed)


def _as(c):
    # The fixture's single app context shares `g` across test clients, and
    # Flask-Login caches the current user there - drop it between users.
    g.pop('_login_user', None)
    return c


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


@pytest.fixture
def setup():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=10))
        client = Client(name='Клиент А')
        db.session.add(client)
        db.session.flush()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([User(username='adm', password=pw, role='admin'),
                            User(username='usr', password=pw, role='regular_user', client_id=client.id)])
        m1, m2 = Machine(name='Лазер'), Machine(name='Преса')
        s1, s2 = Service(name='Рязане', price_per_hour_eur=60), Service(name='Огъване', price_per_hour_eur=40)
        geo = dict(material_key='steel', width=10, height=10, total_length=40, pierce_count=1, calculated_price=1)
        d1, d2 = Detail(name='Пластина', **geo), Detail(name='Скоба', **geo)
        d_own = Detail(name='Частен', owner_client_id=client.id, **geo)
        p1 = Product(name='Стойка', markup_percent=0)
        db.session.add_all([m1, m2, s1, s2, d1, d2, d_own, p1])
        db.session.commit()
        yield dict(client=client, m1=m1, m2=m2, s1=s1, s2=s2, d1=d1, d2=d2, d_own=d_own, p1=p1)
        db.session.remove()
        db.drop_all()


def test_default_is_unrestricted(setup):
    c = setup['client']
    assert client_access(c, 'apps') is None
    assert catalog_item_allowed(setup['d1'], c) and catalog_item_allowed(setup['p1'], c)


def test_admin_saves_limits_and_user_is_limited(setup):
    s = setup
    adm = _login('adm')
    adm.post(f"/admin/clients/{s['client'].id}/access", data={
        'apps_mode': 'only', 'apps': ['upload', 'bogus'],
        'machines_mode': 'only', 'machines': [str(s['m1'].id)],
        'services_mode': 'all',
        'details_mode': 'only', 'details': [str(s['d1'].id)],
        'products_mode': 'only',  # nothing checked -> no public products
    })
    db.session.refresh(s['client'])
    c = s['client']
    assert client_access(c, 'apps') == {'upload'}
    assert client_access(c, 'machines') == {s['m1'].id}
    assert client_access(c, 'services') is None
    assert catalog_item_allowed(s['d1'], c) and not catalog_item_allowed(s['d2'], c)
    assert catalog_item_allowed(s['d_own'], c)  # own private part always visible
    assert not catalog_item_allowed(s['p1'], c)

    usr = _login('usr')
    _as(usr)
    assert usr.get('/generator').status_code == 302
    assert usr.post('/api/generator/dxf', json={}).status_code == 403
    page = usr.get('/upload').get_data(as_text=True)
    assert 'Лазер' in page and 'Преса' not in page
    assert 'Параметричен Генератор' not in page
    order_page = usr.get('/orders/new').get_data(as_text=True)
    assert 'Пластина' in order_page and 'Скоба' not in order_page and 'Стойка' not in order_page

    # staff keep full access
    assert _as(adm).get('/generator').status_code == 200

    # back to "Всички" everywhere clears the limits
    adm.post(f"/admin/clients/{c.id}/access", data={})
    db.session.refresh(c)
    assert c.access_json is None


def test_off_hides_whole_sections(setup):
    s = setup
    from app import Deliverer
    db.session.add(Deliverer(name='КуриерQ7'))
    db.session.commit()
    adm = _login('adm')
    adm.post(f"/admin/clients/{s['client'].id}/access", data={
        'machines_mode': 'off', 'deliverers_mode': 'off', 'services_mode': 'off', 'products_mode': 'off'})
    db.session.refresh(s['client'])
    assert client_access(s['client'], 'deliverers') == set()

    usr = _login('usr')
    _as(usr)
    page = usr.get('/orders/new').get_data(as_text=True)
    assert 'КуриерQ7' not in page and 'Лазер' not in page and 'Рязане' not in page
    assert 'Пластина' in page  # details still on
    assert 'Рязане' not in usr.get('/dashboard').get_data(as_text=True)  # library's services table
    assert 'Рязане' not in usr.get(f"/details/{s['d1'].id}/files").get_data(as_text=True)
    # a tampered post can't pick a hidden machine/courier either
    from app import Order
    usr.post('/orders/new', data={'customer_name': 'X', 'machine_id': str(s['m1'].id), 'deliverer_id': '1',
                                  'cart_json': f'[{{"type": "detail", "id": {s["d1"].id}, "qty": 1}}]'})
    o = Order.query.one()
    assert o.machine_id is None and o.deliverer_id is None


def test_generator_price_button_follows_switches(setup):
    s = setup
    adm = _login('adm')
    usr = _login('usr'); _as(usr)
    assert 'btn-calculate' in usr.get('/generator').get_data(as_text=True)

    def gen(apps):
        _as(adm).post(f"/admin/clients/{s['client'].id}/access", data={'apps_mode': 'only', 'apps': apps})
        _as(usr)
        return usr.get('/generator').get_data(as_text=True)

    assert 'btn-calculate' not in gen(['generator'])                     # files only
    assert 'btn-calculate' in gen(['generator', 'generator_price', 'upload'])
    assert 'btn-calculate' not in gen(['generator', 'generator_price'])  # DXF calculator off
