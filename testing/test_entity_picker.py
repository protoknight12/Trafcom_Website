"""
pytest: the picker window for material / detail / product selects (static/js/entity_picker.js, owner's decision
2026-11-19). Checks that the script is loaded on the key pages, that their entity selects carry data-picker, that
material options carry the picker's data attributes (grade, brand, owner name, stock only for staff) and that the
script parses under node (skipped without node).

Run with:
    python -m pytest testing/test_entity_picker.py -q -p no:cacheprovider
"""
import os
import re
import shutil
import subprocess

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, limiter, User, Client, MaterialPrice, MaterialGrade, Detail, Product,
                 ProductDetail, Offer)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = 'irrelevant123'


@pytest.fixture(scope='module')
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        client = Client(name='Клиент Пикер')
        grade = MaterialGrade(name='Стомана', density_kg_dm3=7.85)
        db.session.add_all([client, grade])
        db.session.flush()
        ours = MaterialPrice(key='ep_steel', display_name='EP Ламарина', type='sheets', cost_per_m2=10, brand='EPBrand',
                             thickness_mm=2, sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=4,
                             grade_id=grade.id, cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        theirs = MaterialPrice(key='ep_client', display_name='EP Клиентска', type='sheets', cost_per_m2=0,
                               thickness_mm=3, stock_quantity=1, owner_client_id=client.id,
                               cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        db.session.add_all([ours, theirs])
        db.session.flush()
        detail = Detail(name='EP Детайл', material_key=ours.key, width=10, height=10, total_length=1, pierce_count=1,
                        calculated_price=5.0, stock_quantity=0, geometry_json='[]')
        db.session.add(detail)
        db.session.flush()
        product = Product(name='EP Продукт', markup_percent=0)
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=1))
        offer = Offer(number='EP-1', object_title='Обект', client_id=client.id)
        admin = User(username='ep_admin', password=generate_password_hash(PASSWORD), role='admin')
        user = User(username='ep_user', password=generate_password_hash(PASSWORD), role='regular_user')
        db.session.add_all([offer, admin, user])
        db.session.commit()
        ids = {'detail': detail.id, 'offer': offer.id}
    yield ids   # outside the app context: flask-login caches the user on g, which a shared context would keep
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def _get(username, url):
    limiter.reset()
    c = flask_app.test_client()
    assert c.post('/login', data={'username': username, 'password': PASSWORD}).status_code in (200, 302)
    res = c.get(url)
    assert res.status_code == 200, (url, res.status_code)
    return res.get_data(as_text=True)


def _select_tag(html, select_id):
    m = re.search(r'<select[^>]*\bid="%s"[^>]*>' % re.escape(select_id), html)
    assert m, select_id
    return m.group(0)


def _option(html, value):
    m = re.search(r'<option value="%s"[^>]*>' % re.escape(value), html)
    assert m, value
    return m.group(0)


@pytest.mark.parametrize('user, url, selects', [
    ('ep_admin', '/admin/delivery-notes', {'dn_material_select': 'material', 'dn_detail_select': 'detail',
                                           'dn_product_select': 'product', 'dn_detail_material_id': 'material',
                                           'dn_product_detail_id': 'detail', 'qd_material': 'material'}),
    ('ep_admin', '/admin/client-delivery-notes', {'cdn_material_select': 'material', 'cdn_detail_select': 'detail',
                                                  'cdn_product_select': 'product'}),
    ('ep_admin', '/orders/new', {'cart_product_id': 'product', 'cart_detail_id': 'detail',
                                 'cart_detail_material': 'material'}),
    ('ep_user', '/orders/new', {'cart_product_id': 'product', 'cart_detail_id': 'detail',
                                'cart_detail_material': 'material'}),
    ('ep_admin', '/admin/offers/{offer}/edit', {'item_product_id': 'product', 'item_detail_id': 'detail'}),
    ('ep_admin', '/details/{detail}/files', {'material_material': 'material', 'alt_material_key': 'material'}),
    ('ep_admin', '/admin/production-orders', {'po_detail_select': 'detail', 'po_material_select': 'material'}),
    ('ep_admin', '/storage/requests', {'reqMaterialSelect': 'material'}),
    ('ep_admin', '/upload', {'material': 'material'}),
])
def test_pages_load_picker(ids, user, url, selects):
    html = _get(user, url.format(**ids))
    assert 'js/entity_picker.js' in html and 'js/cascade_filters.js' in html
    assert 'material_picker.js' not in html
    for select_id, kind in selects.items():
        assert 'data-picker="%s"' % kind in _select_tag(html, select_id), (url, select_id)


def test_material_options_carry_picker_data(ids):
    html = _get('ep_admin', '/admin/delivery-notes')
    opt = _option(html, 'ep_steel')
    for attr in ('data-grade="Стомана"', 'data-brand="EPBrand"', 'data-owner-name="Трафком"', 'data-stock="4"',
                 'data-stock-kg="125.6"', 'data-t="2.0"', 'data-owner=""', 'data-cost="10.00 €/м²"'):
        assert attr in opt, attr
    # the client delivery note lists the client's lot with its owner's name; a grade-less lot reads "Без вид"
    html = _get('ep_admin', '/admin/client-delivery-notes')
    assert re.search(r'<option value="\d+"[^>]*data-grade="Без вид"[^>]*data-owner-name="Клиент Пикер"', html)


def test_customer_never_gets_our_stock(ids):
    html = _get('ep_user', '/upload')
    opt = _option(html, 'ep_steel')
    assert 'data-grade="Стомана"' in opt
    assert 'data-stock' not in opt
    assert 'data-cost' not in opt          # nor our delivery price


def test_script_parses_under_node():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    for name in ('entity_picker.js', 'cascade_filters.js'):
        res = subprocess.run([node, '--check', os.path.join(ROOT, 'static', 'js', name)],
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
