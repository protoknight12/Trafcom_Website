"""
pytest: every row of a material's movement history (admin_material_history()) links to its document - the supplier
/ client receipt print, the client note print, the transfer protocol print (admin only), the production job's row and
the order's card on the production report (none for a deleted order).

Run with:
    pytest testing/test_material_history_links.py -v
"""
from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, Supplier, MaterialPrice, Detail, DeliveryNote, DeliveryNoteItem,
                 ClientDeliveryNote, ClientDeliveryNoteItem, ProductionOrder, Order, OrderItem, MaterialTransfer,
                 MaterialTransferItem, limiter, seed_material_types)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        client = Client(name='Клиент А')
        supplier = Supplier(name='Доставчик')
        steel = MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=25.0, type='sheets',
                              thickness_mm=2.0, sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=10,
                              erp_number=1)
        db.session.add_all([admin, worker, client, supplier, steel])
        db.session.flush()
        detail = Detail(name='Планка', material_key='steel', width=100, height=100, total_length=400, pierce_count=1,
                        calculated_price=1.0,
                        erp_number=2)
        db.session.add(detail)
        db.session.flush()
        dn = DeliveryNote(supplier_id=supplier.id, note_number='DN-1', created_by_id=admin.id)
        cdn = ClientDeliveryNote(client_id=client.id, note_number='CDN-1', kind='issue', created_by_id=admin.id)
        job = ProductionOrder(detail_id=detail.id, material_id=steel.id, quantity=2, planned_material_qty=0.02,
                              actual_material_qty=0.02,
                              status='done', completed_at=datetime.utcnow())
        live = Order(order_number='ORD-LIVE', customer_name='Клиент А', user_id=admin.id)
        gone = Order(order_number='ORD-GONE', customer_name='Клиент А', user_id=admin.id,
                     deleted_at=datetime.utcnow())
        transfer = MaterialTransfer(number=1, to_client_id=client.id, created_by_id=admin.id)
        db.session.add_all([dn, cdn, job, live, gone, transfer])
        db.session.flush()
        db.session.add_all([
            DeliveryNoteItem(delivery_note_id=dn.id, target_type='material', target_id=steel.id, quantity=5,
                             unit_price=50.0, description_snapshot='Стомана'),
            ClientDeliveryNoteItem(client_delivery_note_id=cdn.id, target_type='material', target_id=steel.id,
                                   quantity=1, description_snapshot='Стомана'),
            MaterialTransferItem(transfer_id=transfer.id, source_material_id=steel.id, target_material_id=steel.id,
                                 quantity=1, description_snapshot='Стомана'),
        ] + [OrderItem(order_id=o.id, detail_id=detail.id, quantity_ordered=1,
                       quantity_produced=1, unit_price=1.0, consumed_material_id=steel.id, material_consumed=0.01)
             for o in (live, gone)])
        db.session.commit()
        yield {'steel': steel.id, 'dn': dn.id, 'cdn': cdn.id, 'job': job.id, 'live': live.id, 'gone': gone.id,
               'transfer': transfer.id}
        db.session.remove()
        db.drop_all()


def _page(username, mid):
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    r = c.get(f'/admin/materials/{mid}/history')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_admin_sees_a_link_to_every_document(ids):
    with flask_app.app_context():
        page = _page('qa_admin', ids['steel'])
    assert f'href="/admin/delivery-notes/{ids["dn"]}/print">DN-1</a>' in page
    assert f'href="/admin/client-delivery-notes/{ids["cdn"]}/print">CDN-1</a>' in page
    assert f'href="/admin/production-orders#job-{ids["job"]}">Документ</a>' in page
    assert f'href="/admin/production#order-{ids["live"]}">ORD-LIVE</a>' in page
    assert f'href="/admin/material-transfers/{ids["transfer"]}">' in page
    assert f'#order-{ids["gone"]}' not in page and 'ORD-GONE' in page      # a deleted order has no card


def test_worker_gets_no_link_to_the_admin_only_transfer_print(ids):
    with flask_app.app_context():
        page = _page('qa_worker', ids['steel'])
    assert f'/admin/delivery-notes/{ids["dn"]}/print' in page
    assert '/admin/material-transfers/' not in page


def test_production_job_row_has_the_anchor(ids):
    with flask_app.app_context():
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        assert f'id="job-{ids["job"]}"' in c.get('/admin/production-orders').get_data(as_text=True)
