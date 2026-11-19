"""
pytest: the Доставчици section of /admin/clients - list (with delivery-note count), add (back to the clients
page), edit window + update (ЕИК validated), delete refused while a delivery note or a material request names the
supplier, admin only.

Run with:
    pytest testing/test_suppliers_admin.py -v
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
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Supplier, DeliveryNote, limiter, seed_material_types


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        admin = User(username='qa_admin', password=pw, role='admin')
        db.session.add_all([admin, User(username='qa_worker', password=pw, role='worker')])
        used, free = Supplier(name='Метал М ЕООД', eik='131234567'), Supplier(name='Стари Доставки')
        db.session.add_all([used, free])
        db.session.flush()
        db.session.add(DeliveryNote(supplier_id=used.id, note_number='F-1', created_by_id=admin.id))
        db.session.commit()
        yield dict(used=used.id, free=free.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_list_add_edit_delete(ids):
    c = _login('qa_admin')
    page = c.get('/admin/clients').get_data(as_text=True)
    assert 'Списък с доставчици' in page and 'Метал М ЕООД' in page and 'id="adminSuppliersSearchInput"' in page
    assert f'/admin/suppliers/{ids["used"]}/edit' in page

    res = c.post('/admin/suppliers/add', data={'name': 'Нов Доставчик', 'eik': '204567891', 'back': 'clients'})
    assert res.headers['Location'].endswith('/admin/clients')
    assert c.post('/admin/suppliers/add', data={'name': 'X'}).headers['Location'].endswith('/admin/delivery-notes')

    assert c.get(f'/admin/suppliers/{ids["used"]}/edit').status_code == 200
    c.post(f'/admin/suppliers/{ids["used"]}/update', data={'name': 'Метал М ООД', 'eik': '12', 'phone': '1'})
    with flask_app.app_context():
        assert db.session.get(Supplier, ids['used']).name == 'Метал М ЕООД'      # bad ЕИК -> nothing saved
    c.post(f'/admin/suppliers/{ids["used"]}/update', data={'name': 'Метал М ООД', 'eik': '131234567', 'phone': '0888'})
    with flask_app.app_context():
        s = db.session.get(Supplier, ids['used'])
        assert (s.name, s.phone) == ('Метал М ООД', '0888')

    c.post(f'/admin/suppliers/{ids["used"]}/delete')
    c.post(f'/admin/suppliers/{ids["free"]}/delete')
    with flask_app.app_context():
        assert db.session.get(Supplier, ids['used']) is not None                # has a delivery note -> refused
        assert db.session.get(Supplier, ids['free']) is None


def test_contact_details_saved_listed_and_printed(ids):
    c = _login('qa_admin')
    data = {'name': 'Детайлен ООД', 'back': 'clients', 'address': 'гр. Пловдив, ул. Първа 1',
            'warehouse_address': 'Склад Кукленско шосе', 'mol': 'Петър Петров', 'contact_person': 'Мария',
            'contact_phone': '0899 111 222', 'phone2': '032 123 456', 'website': 'javascript://x%0aalert(1)',
            'bank_account': 'BG80BNBG96611020345678', 'notes': 'Доставя само в петък'}
    c.post('/admin/suppliers/add', data=data)
    with flask_app.app_context():
        s = Supplier.query.filter_by(name='Детайлен ООД').one()
        assert all(getattr(s, k) == v for k, v in data.items() if k not in ('back', 'name'))
        sid = s.id
    page = c.get('/admin/clients').get_data(as_text=True)
    assert 'Склад: Склад Кукленско шосе' in page and 'Контакт: Мария, 0899 111 222' in page and 'Доставя само в петък' in page
    assert 'href="javascript' not in page and 'href="https://javascript://' in page   # never a javascript: link
    edit = c.get(f'/admin/suppliers/{sid}/edit').get_data(as_text=True)
    assert 'BG80BNBG96611020345678' in edit and 'name="notes"' in edit
    c.post(f'/admin/suppliers/{sid}/update', data={'name': 'Детайлен ООД', 'notes': '', 'mol': 'Иван Иванов'})
    with flask_app.app_context():
        s = db.session.get(Supplier, sid)
        assert s.notes is None and s.mol == 'Иван Иванов' and s.address == 'гр. Пловдив, ул. Първа 1'   # absent field kept
        note = DeliveryNote(supplier_id=sid, note_number='F-2', created_by_id=1)
        db.session.add(note)
        db.session.commit()
        note_id = note.id
    printed = c.get(f'/admin/delivery-notes/{note_id}/print').get_data(as_text=True)
    assert 'Адрес: гр. Пловдив, ул. Първа 1' in printed and 'МОЛ: Иван Иванов' in printed


def test_worker_cannot_edit_or_delete(ids):
    c = _login('qa_worker')
    c.post(f'/admin/suppliers/{ids["free"]}/update', data={'name': 'Хак'})
    c.post(f'/admin/suppliers/{ids["free"]}/delete')
    with flask_app.app_context():
        s = db.session.get(Supplier, ids['free'])
        assert s is not None and s.name == 'Стари Доставки'
