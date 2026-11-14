"""
pytest: a Detail private to a client stores its material price with the
OWNER's Материали markup/discount (recomputed when the owner changes), and
detail_price_for() charges the owner that stored price with no Детайли on top.

Run with:
    pytest testing/test_detail_owner_pricing.py -v
"""
import atexit
import io
import json
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
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, OrderItem, limiter,
                 calculate_material_price, detail_price_for)


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
        db.session.add(MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100))
        a = Client(name='Собственик А', material_adjustment_type='markup', material_adjustment_percent=10,
                   detail_adjustment_type='markup', detail_adjustment_percent=20)
        b = Client(name='Собственик Б', material_adjustment_type='discount', material_adjustment_percent=10)
        other = Client(name='Друг', detail_adjustment_type='markup', detail_adjustment_percent=50)
        db.session.add_all([a, b, other])
        db.session.flush()
        pw = generate_password_hash('irrelevant123')
        db.session.add(User(username='adm', password=pw, role='admin'))
        detail = Detail(name='Пластина', material_key='steel', width=1000, height=1000, total_length=40,
                        pierce_count=1, calculated_price=100)
        db.session.add(detail)
        db.session.commit()
        yield dict(a=a, b=b, other=other, detail=detail)
        db.session.remove()
        db.drop_all()


def _admin():
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'adm', 'password': 'irrelevant123'})
    return c


def test_calculate_material_price_applies_client_terms(setup):
    s = setup
    assert calculate_material_price(1000, 1000, 'steel') == 100
    assert calculate_material_price(1000, 1000, 'steel', None) == 100
    assert calculate_material_price(1000, 1000, 'steel', s['a']) == 110
    assert calculate_material_price(1000, 1000, 'steel', s['b']) == 90


def test_admin_add_detail_prices_with_owner(setup):
    s = setup
    adm = _admin()
    for name, owner, expected in (('Публичен', '', 100), ('Мой А', s['a'].id, 110), ('Мой Б', s['b'].id, 90)):
        r = adm.post('/admin/details/add', data={'name': name, 'material': 'steel', 'owner_client_id': str(owner),
                                                 'file': (io.BytesIO(_square_dxf()), 'sq.dxf')},
                     content_type='multipart/form-data')
        assert r.status_code == 302
        assert Detail.query.filter_by(name=name).one().calculated_price == expected


def test_owner_change_reprices(setup):
    s = setup
    adm = _admin()
    d = s['detail']

    def set_owner(owner):
        adm.post(f'/admin/details/{d.id}/owner', data={'owner_client_id': str(owner)})
        db.session.expire_all()
        return db.session.get(Detail, d.id).calculated_price

    assert set_owner(s['a'].id) == 110
    assert set_owner(s['b'].id) == 90
    assert set_owner('') == 100


def test_detail_price_for_owner_skips_detail_terms(setup):
    s = setup
    d = s['detail']
    d.owner_client_id = s['a'].id
    d.calculated_price = calculate_material_price(1000, 1000, 'steel', s['a'])
    db.session.commit()
    assert d.total_price == 110
    assert detail_price_for(d, s['a']) == 110                    # owner: no 20 % Детайли on top
    assert detail_price_for(d, s['other']) == 165                # another client: 110 x 1.5
    assert detail_price_for(d, None) == 110
    public = Detail(name='Публичен', material_key='steel', width=1000, height=1000, total_length=40,
                    pierce_count=1, calculated_price=100)
    db.session.add(public)
    db.session.commit()
    assert detail_price_for(public, s['a']) == 120               # public: owner-to-be still gets his Детайли terms


def test_create_order_for_owner_freezes_stored_price(setup):
    s = setup
    d = s['detail']
    d.owner_client_id = s['a'].id
    d.calculated_price = 110
    db.session.commit()
    adm = _admin()
    adm.post('/orders/new', data={'customer_name': 'X', 'client_id': str(s['a'].id),
                                  'cart_json': json.dumps([{'type': 'detail', 'id': d.id, 'qty': 1}])})
    item = OrderItem.query.one()
    assert item.unit_price == d.total_price == 110


def test_breakdown_shows_owner_rate(setup):
    with flask_app.app_context():
        d = db.session.get(Detail, setup['detail'].id)
        assert d.material_price_breakdown == '1.000 м² × 100.00 €/м²'
        d.owner_client_id = setup['a'].id
        db.session.commit()
        db.session.refresh(d)
        assert d.material_price_breakdown == '1.000 м² × 110.00 €/м², +10% на клиента'
        d.owner_client_id = setup['b'].id
        db.session.commit()
        db.session.refresh(d)
        assert d.material_price_breakdown == '1.000 м² × 90.00 €/м², -10% на клиента'


def test_data_step_reprices_existing_owned_details(setup):
    import runpy
    with flask_app.app_context():
        d = db.session.get(Detail, setup['detail'].id)
        d.owner_client_id = setup['a'].id          # owned before the rule: price still the plain 100
        manual = Detail(name='Ръчна', material_key='steel', width=0, height=0, total_length=0, pierce_count=0, calculated_price=7, owner_client_id=setup['a'].id)
        db.session.add(manual)
        db.session.commit()
    runpy.run_module('migration.data_reprice_owned_details', run_name='__main__')
    with flask_app.app_context():
        assert db.session.get(Detail, setup['detail'].id).calculated_price == 110.0
        assert Detail.query.filter_by(name='Ръчна').one().calculated_price == 7


def test_admin_details_owner_filter(setup):
    s = setup
    s['detail'].owner_client_id = s['a'].id
    db.session.add(Detail(name='Публичен', material_key='steel', width=1, height=1, total_length=0, pierce_count=0, calculated_price=0))
    db.session.commit()
    html = _admin().get('/admin/details').get_data(as_text=True)
    assert 'id="adminDetailsOwnerFilter"' in html
    assert f'<option value="{s["a"].id}">Собственик А</option>' in html
    assert f'<tr data-owner="{s["a"].id}"' in html
    assert '<tr data-owner="public"' in html
    assert 'row.dataset.owner === ownerSel.value' in html
