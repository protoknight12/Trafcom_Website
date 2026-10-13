# -*- coding: utf-8 -*-
"""
pytest regression test for five access-hardening fixes in app.py:

1. ProxyFix - behind nginx the app must see the visitor's address from
   X-Forwarded-For (exactly one trusted hop), otherwise the rate limiter
   puts every visitor in one shared 127.0.0.1 bucket.
2. The AI chat tools (search_catalog, list_products, ...) must not tell a
   client about another client's private Detail/Product, show a customer a
   material's delivery price, or search the internal page='hall' machine cards.
3. upload_detail_dxf() - a customer's .dxf upload is only an attachment; it
   must never fill in a catalog Detail's geometry/price (staff uploads do).
4. update_text() - the public-text editor must refuse 'settings.*' keys
   (internal settings stored in the same EditableText table).

Run with:
    pytest testing/test_access_hardening.py -v
"""
import atexit
import io
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

import ezdxf
import pytest
from flask import request
from flask_login import login_user
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, DetailDxfFile, Product,
                 ServiceMachineCard, EditableText, limiter, search_catalog, list_products,
                 get_product_details, most_expensive_items, get_material_details, get_machine_details)

flask_app.add_url_rule('/_qa/remote-addr', '_qa_remote_addr', lambda: request.remote_addr)


@pytest.fixture
def app(tmp_path, monkeypatch):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    # Uploads of this test go to a throwaway folder, not the real detail_dxf_files/.
    monkeypatch.setitem(flask_app.config, 'DETAIL_DXF_FOLDER', str(tmp_path))
    limiter.reset()
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        client_a, client_b = Client(name='Клиент А'), Client(name='Клиент Б')
        db.session.add_all([client_a, client_b])
        db.session.flush()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([
            User(username='qa_client_a', password=pw, role='regular_user', client_id=client_a.id),
            User(username='qa_worker', password=pw, role='worker'),
            User(username='qa_designer', password=pw, role='web_designer'),
            MaterialPrice(key='qa_mat', display_name='QA Стомана', type='sheets', cost_per_m2=12.5,
                          cutting_speed_mm_per_min=1000, pierce_rate_per_min=30),
            Detail(name='Скоба публична', material_key='qa_mat', width=0, height=0, total_length=0,
                   pierce_count=0, calculated_price=5.0),
            Detail(name='Скоба частна Б', material_key='qa_mat', width=0, height=0, total_length=0,
                   pierce_count=0, calculated_price=9.0, owner_client_id=client_b.id),
            Product(name='Шкаф публичен', markup_percent=0),
            Product(name='Шкаф частен Б', markup_percent=0, owner_client_id=client_b.id),
            ServiceMachineCard(page='services', title='Лазер публичен'),
            ServiceMachineCard(page='hall', title='Вътрешен абкант', specs_text='Сериен №: 12345'),
        ])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


def _browser(app, username):
    c = app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def _as(username, tool, *args):
    """Calls a chat tool's plain function the way /api/chat's tool runner does - inside a request of that user."""
    with flask_app.test_request_context():
        login_user(User.query.filter_by(username=username).first())
        return tool.func(*args)


def _sample_dxf_bytes(w=100.0, h=50.0):
    doc = ezdxf.new('R2000')
    doc.modelspace().add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


def test_remote_addr_comes_from_the_one_trusted_proxy_hop(app):
    c = app.test_client()
    assert c.get('/_qa/remote-addr', headers={'X-Forwarded-For': '203.0.113.7'}).get_data(as_text=True) == '203.0.113.7'
    # nginx appends the real peer last - anything the visitor put in front of it is not trusted.
    spoofed = c.get('/_qa/remote-addr', headers={'X-Forwarded-For': '10.0.0.1, 203.0.113.7'})
    assert spoofed.get_data(as_text=True) == '203.0.113.7'


def test_chat_tools_hide_other_clients_private_catalog(app):
    for tool, args in ((search_catalog, ('Скоба',)), (search_catalog, ('Шкаф',)), (list_products, ()),
                       (most_expensive_items, ()), (get_product_details, ('частен',))):
        seen = _as('qa_client_a', tool, *args)
        assert 'Шкаф частен Б' not in seen and 'Скоба частна Б' not in seen
    assert 'Скоба публична' in _as('qa_client_a', search_catalog, 'Скоба')
    assert 'Шкаф публичен' in _as('qa_client_a', list_products)

    assert 'Скоба частна Б' in _as('qa_worker', search_catalog, 'Скоба')
    assert 'Шкаф частен Б' in _as('qa_worker', list_products)
    assert 'Шкаф частен Б' in _as('qa_worker', get_product_details, 'Шкаф частен Б')
    assert 'Скоба частна Б' in _as('qa_worker', most_expensive_items)


def test_chat_tools_hide_delivery_price_and_hall_cards_from_customers(app):
    customer, staff = _as('qa_client_a', get_material_details, 'QA Стомана'), _as('qa_worker', get_material_details, 'QA Стомана')
    assert 'QA Стомана' in customer and '12.5' not in customer
    assert '12.5' in staff
    # the ranking of materials is by delivery price too - a customer gets the details part only
    customer, staff = _as('qa_client_a', most_expensive_items), _as('qa_worker', most_expensive_items)
    assert 'QA Стомана' not in customer and 'Скоба публична' in customer
    assert 'QA Стомана' in staff and '12.5' in staff
    assert '12345' not in _as('qa_client_a', get_machine_details, 'Вътрешен абкант')
    assert 'Лазер публичен' in _as('qa_client_a', get_machine_details, 'Лазер')
    assert '12345' in _as('qa_worker', get_machine_details, 'Вътрешен абкант')


def test_product_details_do_not_name_other_clients_private_components(app):
    from app import ProductDetail
    mixed = Product(name='Шкаф смесен', markup_percent=0)
    db.session.add(mixed)
    db.session.flush()
    for name in ('Скоба публична', 'Скоба частна Б'):
        db.session.add(ProductDetail(product_id=mixed.id, detail_id=Detail.query.filter_by(name=name).first().id, quantity=2))
    db.session.commit()

    customer = _as('qa_client_a', get_product_details, 'Шкаф смесен')
    assert 'Скоба публична x2' in customer and 'Скоба частна Б' not in customer
    assert 'и 1 други компонента' in customer
    staff = _as('qa_worker', get_product_details, 'Шкаф смесен')
    assert 'Скоба частна Б x2' in staff and 'други компонента' not in staff


def test_customer_detail_upload_is_limited_to_allowed_file_types(app):
    detail_id = Detail.query.filter_by(name='Скоба публична').first().id
    post = lambda username, name: _browser(app, username).post(
        f'/details/{detail_id}/files/upload', data={'file': (io.BytesIO(b'x'), name)}, content_type='multipart/form-data')
    assert post('qa_client_a', 'run.exe').status_code == 302
    assert DetailDxfFile.query.filter_by(detail_id=detail_id).count() == 0
    post('qa_client_a', 'photo.JPG')
    assert DetailDxfFile.query.filter_by(detail_id=detail_id).count() == 1


def test_staff_detail_upload_is_not_limited_by_file_type(app):
    # Own test: the fixture's app context shares flask-login's cached user between browsers.
    detail_id = Detail.query.filter_by(name='Скоба публична').first().id
    _browser(app, 'qa_worker').post(f'/details/{detail_id}/files/upload', data={'file': (io.BytesIO(b'x'), 'notes.txt')},
                                    content_type='multipart/form-data')
    assert DetailDxfFile.query.filter_by(detail_id=detail_id).count() == 1


def _upload_dxf(app, username):
    detail_id = Detail.query.filter_by(name='Скоба публична').first().id
    data = {'file': (io.BytesIO(_sample_dxf_bytes(100.0, 50.0)), 'part.dxf')}
    res = _browser(app, username).post(f'/details/{detail_id}/files/upload', data=data, content_type='multipart/form-data')
    assert res.status_code == 302
    db.session.expire_all()
    assert DetailDxfFile.query.filter_by(detail_id=detail_id).count() == 1  # kept as an attachment either way
    return db.session.get(Detail, detail_id)


def test_customer_dxf_upload_never_changes_catalog_geometry_or_price(app):
    detail = _upload_dxf(app, 'qa_client_a')
    assert (detail.total_length, detail.calculated_price, detail.geometry_json) == (0, 5.0, None)


def test_staff_dxf_upload_still_fills_missing_geometry(app):
    # Own test (not a second login above): the fixture's app context shares flask-login's cached user between browsers.
    assert _upload_dxf(app, 'qa_worker').total_length == 300.0  # perimeter of a 100x50 rectangle


def test_text_editor_refuses_internal_settings_keys(app):
    designer = _browser(app, 'qa_designer')
    designer.post('/content-text/settings.registration_closed/update', data={'content': '1'})
    assert db.session.get(EditableText, 'settings.registration_closed') is None
    assert designer.get('/content-text/edit?key=settings.heatpump_host').status_code == 302

    designer.post('/content-text/index.hero_lead/update', data={'content': 'Нов текст'})
    assert db.session.get(EditableText, 'index.hero_lead').content == 'Нов текст'
