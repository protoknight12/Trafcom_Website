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
