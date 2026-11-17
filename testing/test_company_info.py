"""
pytest: our company's data (/admin/company, company() in app.py, EditableText 'settings.company.*') - defaults
without rows, an admin saves them, every document prints them (delivery-note prints, offer, quality, certificate,
label, protocol), bank accounts one per line, the VAT rate drives "ДДС n%" / "ОБЩО С ДДС"; workers and the content
editor cannot change them.

Run with:
    pytest testing/test_company_info.py -v
"""
import re

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, Supplier, MaterialPrice, Product, DeliveryNote, DeliveryNoteItem,
                 ClientDeliveryNote, ClientDeliveryNoteItem, EditableText, limiter, seed_material_types, company)

DATA = {'name': 'ТЕСТ ФИРМА ЕООД', 'address': 'гр. Пробен, ул. Тестова 1', 'production_address': 'ул. Цехова 2',
        'eik': '123456789', 'vat_number': 'BG123456789', 'mol': 'Петър Петров', 'phone': '+359 1', 'email': 'a@b.bg',
        'website': 'b.bg', 'bank_accounts': 'ОББ, BG11UBBS1, UBBSBGSF\r\n\r\nДСК, BG22STSA2, STSABGSF',
        'vat_percent': '9'}


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add_all([User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'),
                            User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker'),
                            User(username='qa_web', password=generate_password_hash('irrelevant123'), role='web_designer')])
        client, supplier = Client(name='Клиент'), Supplier(name='Доставчик')
        mat = MaterialPrice(key='m', display_name='Мат', cost_per_m2=0, type='sheets')
        product = Product(name='Изделие', markup_percent=0)
        db.session.add_all([client, supplier, mat, product])
        db.session.flush()
        sup = DeliveryNote(supplier_id=supplier.id)
        issue = ClientDeliveryNote(client_id=client.id, kind='issue')
        db.session.add_all([sup, issue])
        db.session.flush()
        db.session.add(DeliveryNoteItem(delivery_note_id=sup.id, target_type='material', target_id=mat.id,
                                        description_snapshot='Мат', quantity=1, unit_price=100))
        db.session.add(ClientDeliveryNoteItem(client_delivery_note_id=issue.id, target_type='material',
                                              target_id=mat.id, description_snapshot='Мат', quantity=1, unit_price=100))
        db.session.commit()
        yield dict(sup=sup.id, issue=issue.id, product=product.id, mat=mat.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    from flask import g
    g.pop('_login_user', None)
    g.pop('company_info', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def _text(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))


def test_defaults_without_rows(ids):
    from flask import g
    g.pop('company_info', None)
    info = company()
    assert info['name'] == 'ТРАФКОМ ООД' and info['eik'] == '110034354' and info['vat'] == 20 and info['accounts'] == []
    html = _login('qa_admin').get(f"/admin/delivery-notes/{ids['sup']}/print").get_data(as_text=True)
    assert 'ТРАФКОМ ООД' in html and 'ДДС 20%: 20.00 EUR' in _text(html)


def test_admin_saves_and_documents_use_it(ids):
    c = _login('qa_admin')
    assert c.post('/admin/company', data=DATA).status_code == 302
    from flask import g
    g.pop('company_info', None)
    assert company()['accounts'] == ['ОББ, BG11UBBS1, UBBSBGSF', 'ДСК, BG22STSA2, STSABGSF']
    page = c.get('/admin/company').get_data(as_text=True)
    assert 'value="ТЕСТ ФИРМА ЕООД"' in page

    issue = _text(c.get(f"/admin/client-delivery-notes/{ids['issue']}/print").get_data(as_text=True))
    assert 'ТЕСТ ФИРМА ЕООД' in issue and 'ЕИК: 123456789' in issue and 'МОЛ: Петър Петров' in issue
    assert 'ДДС 9%: 9.00 EUR' in issue and 'ОБЩО С ДДС: 109.00 EUR' in issue and 'BG22STSA2' in issue
    assert 'ТРАФКОМ' not in issue
    for url in (f"/admin/products/{ids['product']}/protocol", f"/admin/products/{ids['product']}/certificates",
                f"/admin/print-label/material/{ids['mat']}"):
        html = c.get(url).get_data(as_text=True)
        assert 'ТЕСТ ФИРМА ЕООД' in html and 'ТРАФКОМ ООД' not in html, url
    protocol = c.get(f"/admin/products/{ids['product']}/protocol").get_data(as_text=True)
    assert 'value="Петър Петров"' in protocol and 'ЕИК: 123456789' in protocol


def test_bad_vat_refused(ids):
    c = _login('qa_admin')
    c.post('/admin/company', data=dict(DATA, vat_percent='abc'))
    from flask import g
    g.pop('company_info', None)
    assert company()['name'] == 'ТРАФКОМ ООД'


def test_only_admins(ids):
    for user in ('qa_worker', 'qa_web'):
        c = _login(user)
        c.post('/admin/company', data=DATA)
        c.post('/content-text/settings.company.name/update', data={'content': 'Хакер'})
    with flask_app.app_context():
        assert EditableText.query.filter(EditableText.key.startswith('settings.company.')).count() == 0
