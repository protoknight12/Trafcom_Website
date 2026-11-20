"""
pytest: invoicing of a client delivery note (owner's decision 2026-11-20) - "Фактурирано" + invoice № / date and
"Платено" from the note dialog (create / edit) and the list's "Фактура" (client_delivery_note_invoice(), no stock
movement); unticking "Фактурирано" clears the number and date; a bad date is refused; the list shows the status and
the print page the invoice under the ref №; workers cannot set it.

Run with:
    pytest testing/test_client_note_invoice.py -v
"""
import json
from datetime import date

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, Client, ClientDeliveryNote, limiter,
                 seed_material_types)


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([User(username='inv_admin', password=pw, role='admin'),
                            User(username='inv_worker', password=pw, role='worker')])
        lot = MaterialPrice(key='inv_steel', display_name='Стомана', type='sheets', cost_per_m2=10, stock_quantity=10,
                            sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2)
        client = Client(name='Клиент Фактура')
        db.session.add_all([lot, client])
        db.session.commit()
        yield dict(lot=lot.id, client=client.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def _note():
    db.session.expire_all()
    return ClientDeliveryNote.query.one()


def _stock(lot_id):
    db.session.expire_all()
    return db.session.get(MaterialPrice, lot_id).stock_quantity


def test_invoice_fields_on_create_quick_dialog_list_and_print(s):
    c = _login('inv_admin')
    lines = json.dumps([{'type': 'material', 'target_id': s['lot'], 'qty': 2, 'unit_price': 12}])
    c.post('/admin/client-delivery-notes/create', data={
        'client_id': str(s['client']), 'kind': 'issue', 'note_number': 'REF-7', 'items_json': lines,
        'invoiced': '1', 'invoice_number': 'F-100', 'invoice_date': '2026-11-20'})
    note = _note()
    assert (note.invoiced, note.invoice_number, note.invoice_date, note.paid) == (True, 'F-100', date(2026, 11, 20), False)
    assert _stock(s['lot']) == 8

    # "Фактура" on the list: paid, other number - no stock movement
    c.post(f'/admin/client-delivery-notes/{note.id}/invoice',
           data={'invoiced': '1', 'invoice_number': 'F-101', 'invoice_date': '2026-11-21', 'paid': '1'})
    note = _note()
    assert (note.invoice_number, note.invoice_date, note.paid) == ('F-101', date(2026, 11, 21), True)
    assert _stock(s['lot']) == 8

    page = c.get('/admin/client-delivery-notes').get_data(as_text=True)
    assert 'Фактурирано' in page and 'F-101' in page and 'data-invoice="paid"' in page and 'id="filterInvoice"' in page
    printed = c.get(f'/admin/client-delivery-notes/{note.id}/print').get_data(as_text=True)
    assert printed.index('REF-7') < printed.index('F-101') and '21.11.2026' in printed

    # unticking "Фактурирано" clears the number and date; "Платено" stays independent
    c.post(f'/admin/client-delivery-notes/{note.id}/invoice', data={'invoice_number': 'X', 'paid': '1'})
    note = _note()
    assert (note.invoiced, note.invoice_number, note.invoice_date, note.paid) == (False, None, None, True)
    assert 'F-10' not in c.get(f'/admin/client-delivery-notes/{note.id}/print').get_data(as_text=True)


def test_bad_date_refused_and_worker_cannot_set(s):
    c = _login('inv_admin')
    lines = json.dumps([{'type': 'material', 'target_id': s['lot'], 'qty': 1}])
    c.post('/admin/client-delivery-notes/create', data={'client_id': str(s['client']), 'items_json': lines})
    note = _note()
    assert (note.invoiced, note.paid) == (False, False)
    c.post(f'/admin/client-delivery-notes/{note.id}/invoice', data={'invoiced': '1', 'invoice_date': '20.11.2026'})
    assert _note().invoiced is False
    res = _login('inv_worker').post(f'/admin/client-delivery-notes/{note.id}/invoice', data={'invoiced': '1'})
    assert res.status_code in (302, 403)
    assert _note().invoiced is False


def test_list_total_and_declarations_per_detail(s):
    """The list shows the note's total without VAT; "Декларации за съответствие" prints one page per detail line
    (pre-filled name / КД № / quantity / client), none for a material line; a note without details redirects."""
    from app import Detail
    d = Detail(name='Скоба', code_number='КД-77', material_key='inv_steel', width=100, height=50, total_length=1,
               pierce_count=1, calculated_price=3.0, stock_quantity=5)
    db.session.add(d)
    db.session.commit()
    c = _login('inv_admin')
    lines = json.dumps([{'type': 'material', 'target_id': s['lot'], 'qty': 2, 'unit_price': 12},
                        {'type': 'detail', 'target_id': d.id, 'qty': 3, 'unit_price': 5}])
    c.post('/admin/client-delivery-notes/create', data={'client_id': str(s['client']), 'items_json': lines})
    note = _note()
    assert 'Общо: 39.00 €' in c.get('/admin/client-delivery-notes').get_data(as_text=True)
    assert 'Декларации за съответствие' in c.get(f'/admin/client-delivery-notes/{note.id}/print').get_data(as_text=True)
    page = _login('inv_worker').get(f'/admin/client-delivery-notes/{note.id}/declarations').get_data(as_text=True)
    assert page.count('class="doc-sheet"') == 1                 # the detail only
    assert 'Скоба' in page and 'КД-77' in page and '3 бр.' in page and 'Клиент Фактура' in page

    only_material = json.dumps([{'type': 'material', 'target_id': s['lot'], 'qty': 1}])
    c = _login('inv_admin')
    c.post('/admin/client-delivery-notes/create', data={'client_id': str(s['client']), 'items_json': only_material})
    db.session.expire_all()
    other = ClientDeliveryNote.query.order_by(ClientDeliveryNote.id.desc()).first()
    res = c.get(f'/admin/client-delivery-notes/{other.id}/declarations')
    assert res.status_code == 302 and res.headers['Location'].endswith(f'/admin/client-delivery-notes/{other.id}/print')


def test_declaration_numbers_are_common_and_stable(s):
    """One numbering "2" + 7 digits for every declaration: a note's detail line gets its number on the first open and
    keeps it on a reprint; the product certificate's "Нов номер" continues the same sequence."""
    import re
    from app import Detail, Product, ConformityDeclaration
    d = Detail(name='Скоба', material_key='inv_steel', width=100, height=50, total_length=1, pierce_count=1,
               calculated_price=3.0, stock_quantity=5)
    p = Product(name='Изделие', markup_percent=0)
    db.session.add_all([d, p])
    db.session.commit()
    c = _login('inv_admin')
    lines = json.dumps([{'type': 'detail', 'target_id': d.id, 'qty': 1}, {'type': 'detail', 'target_id': d.id, 'qty': 2}])
    c.post('/admin/client-delivery-notes/create', data={'client_id': str(s['client']), 'items_json': lines})
    note = _note()
    url = f'/admin/client-delivery-notes/{note.id}/declarations'
    first = c.get(url).get_data(as_text=True)
    assert first.count('class="doc-sheet"') == 1 and '3 бр.' in first      # two lines of one detail = one declaration
    assert re.findall(r'\b2\d{7}\b', first) == ['20000001']
    assert re.findall(r'\b2\d{7}\b', c.get(url).get_data(as_text=True)) == ['20000001']   # reprint: same number
    res = c.post('/admin/declarations/new', json={'product_id': p.id, 'name': 'Скоба 2', 'quantity': 4})
    assert res.get_json() == {'number': '20000002'}
    assert ConformityDeclaration.query.count() == 2
    assert c.post('/admin/declarations/new', json={}).status_code == 400
    assert 'btnNewCertNum' in c.get(f'/admin/products/{p.id}/certificates').get_data(as_text=True)


def test_invoice_pdf_attach_replace_remove(s):
    """"Фактурирано" + a PDF: stored under INVOICE_FILE_FOLDER and served to staff; a non-PDF is refused; a new PDF
    replaces the old (old file removed); "Премахни" / unticking "Фактурирано" drops it; deleting the note removes it."""
    import io, os
    folder = flask_app.config['INVOICE_FILE_FOLDER']
    c = _login('inv_admin')
    lines = json.dumps([{'type': 'material', 'target_id': s['lot'], 'qty': 1}])
    c.post('/admin/client-delivery-notes/create', data={'client_id': str(s['client']), 'items_json': lines})
    note = _note()
    url = f'/admin/client-delivery-notes/{note.id}/invoice'

    c.post(url, data={'invoiced': '1', 'invoice_pdf': (io.BytesIO(b'hello'), 'f.pdf')}, content_type='multipart/form-data')
    assert _note().invoice_file is None                                   # not a PDF

    c.post(url, data={'invoiced': '1', 'invoice_pdf': (io.BytesIO(b'%PDF-1.4 one'), 'Фактура 1.pdf')},
           content_type='multipart/form-data')
    first = _note().invoice_file
    assert first and _note().invoice_file_name == 'Фактура 1.pdf' and os.path.exists(os.path.join(folder, first))
    res = c.get(f'/admin/client-delivery-notes/{note.id}/invoice.pdf')
    assert res.status_code == 200 and res.data.startswith(b'%PDF-1.4 one')
    res.close()
    assert 'Фактура (PDF)</a>' in c.get('/admin/client-delivery-notes').get_data(as_text=True)

    c.post(url, data={'invoiced': '1', 'invoice_pdf': (io.BytesIO(b'%PDF-1.4 two'), 'two.pdf')},
           content_type='multipart/form-data')
    second = _note().invoice_file
    assert second != first and not os.path.exists(os.path.join(folder, first))

    c.post(url, data={'invoiced': '1', 'invoice_file_remove': '1'})
    assert _note().invoice_file is None and not os.path.exists(os.path.join(folder, second))
    assert c.get(f'/admin/client-delivery-notes/{note.id}/invoice.pdf').status_code == 404

    c.post(url, data={'invoiced': '1', 'invoice_pdf': (io.BytesIO(b'%PDF-1.4 three'), 'three.pdf')},
           content_type='multipart/form-data')
    third = _note().invoice_file
    c.post(url, data={})                                                    # unticked: drops the PDF too
    assert _note().invoice_file is None and not os.path.exists(os.path.join(folder, third))

    c.post(url, data={'invoiced': '1', 'invoice_pdf': (io.BytesIO(b'%PDF-1.4 four'), 'four.pdf')},
           content_type='multipart/form-data')
    fourth = _note().invoice_file
    c.post(f'/admin/client-delivery-notes/{note.id}/delete')
    assert not os.path.exists(os.path.join(folder, fourth))
