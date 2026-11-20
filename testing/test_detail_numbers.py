"""
pytest: the three numbers of a detail (owner's decision 2026-11-20) - our nomenclature № (erp_number), the client's
(external_number) and КД № (code_number): set when the detail is created (dialog + quick create), edited on the
detail page ("Номера", admin_update_detail_numbers - blank ERP = the next free one, a taken one refused), listed
and searchable on /admin/details, admin only.

Run with:
    pytest testing/test_detail_numbers.py -v
"""
import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, limiter, seed_material_types


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([User(username='dn_admin', password=pw, role='admin'),
                            User(username='dn_worker', password=pw, role='worker')])
        db.session.add(MaterialPrice(key='dn_steel', display_name='Стомана', type='sheets', cost_per_m2=10,
                                     sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2,
                                     cutting_speed_mm_per_min=1000, pierce_rate_per_min=60, erp_number=500))
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_numbers_on_create_edit_and_list(s):
    c = _login('dn_admin')
    c.post('/admin/details/add', data={'name': 'Планка', 'material': 'dn_steel', 'manual_price': '1',
                                      'code_number': 'КД-1', 'external_number': 'CLIENT-77'})
    d = Detail.query.filter_by(name='Планка').one()
    assert (d.code_number, d.external_number) == ('КД-1', 'CLIENT-77') and d.erp_number

    res = c.post('/api/quick-create-detail', data={'name': 'Скоба', 'material': 'dn_steel', 'manual_price': '2',
                                                   'external_number': 'C-88'})
    assert res.status_code == 200
    assert Detail.query.filter_by(name='Скоба').one().external_number == 'C-88'

    url = f'/admin/details/{d.id}/numbers'
    c.post(url, data={'erp_number': '500', 'external_number': 'X', 'code_number': 'Y'})   # 500 = the material's
    db.session.expire_all()
    assert db.session.get(Detail, d.id).external_number == 'CLIENT-77'                    # refused, unchanged
    c.post(url, data={'erp_number': '', 'external_number': ' CLIENT-78 ', 'code_number': 'КД-2'})
    db.session.expire_all()
    d = db.session.get(Detail, d.id)
    assert (d.external_number, d.code_number) == ('CLIENT-78', 'КД-2') and d.erp_number   # blank ERP = next free

    page = c.get('/admin/details').get_data(as_text=True)
    assert 'Наш № / Външен № / КД №' in page and 'CLIENT-78' in page
    assert 'id="num_external"' in c.get(f'/details/{d.id}/files').get_data(as_text=True)

    res = _login('dn_worker').post(url, data={'external_number': 'HACK'})
    assert res.status_code in (302, 403)
    db.session.expire_all()
    assert db.session.get(Detail, d.id).external_number == 'CLIENT-78'


def test_numbers_shown_on_documents(s):
    """catalog_numbers(): "Наш № · Вън. № · КД №" under the name in storage, delivery notes (list + print),
    orders (production report) - '' when nothing is set."""
    import json
    from app import catalog_numbers, ClientDeliveryNote, Client
    c = _login('dn_admin')
    c.post('/admin/details/add', data={'name': 'Планка', 'material': 'dn_steel', 'manual_price': '1',
                                      'erp_number': '901', 'code_number': 'КД-1', 'external_number': 'CL-77'})
    d = Detail.query.filter_by(name='Планка').one()
    assert catalog_numbers(d) == 'Наш № 901 · Вън. № CL-77 · КД № КД-1'
    assert catalog_numbers(Detail(name='x')) == ''
    text = 'Наш № 901 · Вън. № CL-77 · КД № КД-1'

    assert text in c.get('/storage/details').get_data(as_text=True)
    client = Client(name='Клиент')
    db.session.add(client)
    db.session.commit()
    c.post('/admin/client-delivery-notes/create', data={
        'client_id': str(client.id), 'items_json': json.dumps([{'type': 'detail', 'target_id': d.id, 'qty': 1}])})
    note = ClientDeliveryNote.query.one()
    assert text in c.get('/admin/client-delivery-notes').get_data(as_text=True)
    assert text in c.get(f'/admin/client-delivery-notes/{note.id}/print').get_data(as_text=True)
    c.post('/orders/new', data={'customer_name': 'QA', 'client_id': str(client.id),
                                'cart_json': json.dumps([{'type': 'detail', 'id': d.id, 'qty': 1}])})
    assert text in c.get('/admin/production').get_data(as_text=True)
