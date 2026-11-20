"""
pytest: the operation price is visible BEFORE "+ Добави в списъка" (owner's decision 2026-11-20) - the detail page
and the new-detail dialog carry the live preview and the per-service pricing data (time: €/час, length: €/м); the
new-detail dialog now also takes length-priced operations (length_mm), stored like on the detail page.

Run with:
    pytest testing/test_operation_price_preview.py -v
"""
import json
import re

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, Service, Operation, limiter, seed_material_types


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='opp_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        material = MaterialPrice(key='opp_steel', display_name='Стомана', type='sheets', cost_per_m2=10,
                                 sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2,
                                 cutting_speed_mm_per_min=1000, pierce_rate_per_min=60)
        laser = Service(name='Лазер', pricing_mode='length', price_per_meter_eur=2.5, price_per_hour_eur=0)
        bend = Service(name='Огъване', pricing_mode='time', price_per_hour_eur=60)
        db.session.add_all([material, laser, bend])
        db.session.flush()
        detail = Detail(name='Планка', material_key='opp_steel', width=100, height=100, total_length=400,
                        pierce_count=1, calculated_price=1.0)
        db.session.add(detail)
        db.session.commit()
        yield dict(detail=detail.id, laser=laser.id, bend=bend.id)
        db.session.remove()
        db.drop_all()


def _admin():
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'opp_admin', 'password': 'irrelevant123'})
    return c


def test_preview_elements_and_service_data(ids):
    c = _admin()
    page = c.get(f'/details/{ids["detail"]}/files').get_data(as_text=True)
    assert 'id="op_price_preview"' in page and 'function updateOpPricePreview' in page
    listing = c.get('/admin/details').get_data(as_text=True)
    assert 'id="nd_op_price_preview"' in listing and 'id="nd_op_length_mm"' in listing
    services = json.loads(re.search(r'const ND_SERVICES = (\[.*?\]);', listing).group(1))
    laser = next(s for s in services if s['id'] == ids['laser'])
    assert laser['pricing_mode'] == 'length' and laser['price_per_meter_eur'] == 2.5
    assert '2.50 €/м' in listing                                         # the option shows the length rate


def test_new_detail_dialog_stores_a_length_operation(ids):
    c = _admin()
    ops = [{'service_id': ids['laser'], 'length_mm': 800, 'duration_minutes': None},
           {'service_id': ids['bend'], 'duration_minutes': 3}]
    c.post('/admin/details/add', data={'name': 'Нов детайл', 'material': 'opp_steel', 'manual_price': '1',
                                      'operations_json': json.dumps(ops)})
    detail = Detail.query.filter_by(name='Нов детайл').one()
    rows = sorted((o.service_id, o.length_mm, o.duration_minutes) for o in Operation.query.filter_by(detail_id=detail.id))
    assert rows == sorted([(ids['laser'], 800, 0.0), (ids['bend'], None, 3.0)])
    costs = sorted(o.cost for o in Operation.query.filter_by(detail_id=detail.id))
    assert costs == [2.0, 3.0]                                           # 0.8 m x 2.50 / 3 min x 60 €/h


def test_piece_priced_service_and_operation(ids):
    """"По брой" (owner's decision 2026-11-20): a service with pricing_mode 'piece' and price_per_piece_eur; an
    operation stores `pieces` and costs pieces x €/бр.; the pages list it with its €/бр. rate and the preview."""
    c = _admin()
    c.post('/admin/services/add', data={'name': 'Резба', 'price_per_hour_eur': '30', 'pricing_mode': 'piece',
                                        'price_per_piece_eur': '0.45'})
    thread = Service.query.filter_by(name='Резба').one()
    assert (thread.pricing_mode, thread.price_per_piece_eur, thread.price_per_meter_eur) == ('piece', 0.45, None)
    c.post('/admin/services/add', data={'name': 'Без цена', 'price_per_hour_eur': '30', 'pricing_mode': 'piece'})
    assert Service.query.filter_by(name='Без цена').first() is None          # the €/бр. rate is required

    c.post(f'/admin/details/{ids["detail"]}/operations/add',
           data={'operations_json': json.dumps([{'service_id': thread.id, 'pieces': 4}])})
    op = Operation.query.filter_by(detail_id=ids['detail'], service_id=thread.id).one()
    assert (op.pieces, op.cost) == (4, 1.8)                                 # 4 x 0.45
    page = c.get(f'/details/{ids["detail"]}/files').get_data(as_text=True)
    assert 'id="op_pieces"' in page and '0.45 €/бр.' in page and '4 бр.' in page
    assert '0.45 / бр.' in c.get('/admin/services').get_data(as_text=True)

    # switching the service to length clears the per-piece rate
    c.post(f'/admin/services/{thread.id}/update', data={'name': 'Резба', 'price_per_hour_eur': '30',
                                                       'pricing_mode': 'length', 'price_per_meter_eur': '2'})
    db.session.expire_all()
    thread = db.session.get(Service, thread.id)
    assert (thread.pricing_mode, thread.price_per_piece_eur, thread.price_per_meter_eur) == ('length', None, 2.0)
