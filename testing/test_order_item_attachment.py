"""
pytest regression test for the reference-PDF attachment picked in the
detail+operations section on order_create.html - see OrderItemAttachment,
upload_order_item_pdf() (stages the file before the OrderItem exists),
_link_order_item_attachment() (links it once create_order() flushes the
OrderItem), and download_order_item_file() (admin/staff-only download) in
app.py. Uses the Flask test client since this needs real request/session/
login/file-upload behavior.

Run with:
    pytest testing/test_order_item_attachment.py -v
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

import json

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, Order, OrderItem, OrderItemAttachment, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        user = User(username='qa_user', password=generate_password_hash('irrelevant123'), role='regular_user')
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        db.session.add_all([user, worker])
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10,
                                  cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        db.session.add(material)
        db.session.flush()
        hinge = Detail(name='Алуминиева панта', material_key=material.key, width=10, height=10,
                        total_length=1, pierce_count=1, calculated_price=5.0)
        db.session.add(hinge)
        db.session.commit()
        yield flask_app, hinge.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, hinge_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_user', 'password': 'irrelevant123'})
    return c, hinge_id


def _fake_pdf(name='drawing.pdf'):
    return (io.BytesIO(b'%PDF-1.4 fake content'), name)


def test_stage_and_link_pdf_attachment(client):
    c, hinge_id = client
    res = c.post('/api/upload-order-item-pdf', data={'file': _fake_pdf()}, content_type='multipart/form-data')
    assert res.status_code == 200
    staged = res.get_json()
    assert staged['status'] == 'success'
    assert staged['filename'].endswith('_drawing.pdf')

    cart = [{
        'type': 'detail', 'id': hinge_id, 'qty': 1,
        'attachment': {'filename': staged['filename'], 'original_filename': staged['original_filename']},
    }]
    res = c.post('/orders/new', data={'customer_name': 'QA Customer', 'cart_json': json.dumps(cart)})
    assert res.status_code == 302

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer').first()
        item = OrderItem.query.filter_by(order_id=order.id).first()
        attachments = OrderItemAttachment.query.filter_by(order_item_id=item.id).all()
        assert len(attachments) == 1
        assert attachments[0].original_filename == 'drawing.pdf'


def test_non_pdf_upload_rejected(client):
    c, _hinge_id = client
    res = c.post('/api/upload-order-item-pdf', data={'file': (io.BytesIO(b'hello'), 'notes.txt')},
                  content_type='multipart/form-data')
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'


def test_bogus_attachment_filename_is_ignored(client):
    c, hinge_id = client
    # Never staged via the upload endpoint - a client crafting cart_json by
    # hand shouldn't be able to link an arbitrary/unstaged filename.
    cart = [{
        'type': 'detail', 'id': hinge_id, 'qty': 1,
        'attachment': {'filename': '../../etc/passwd', 'original_filename': 'passwd'},
    }]
    res = c.post('/orders/new', data={'customer_name': 'QA Customer Bogus', 'cart_json': json.dumps(cart)})
    assert res.status_code == 302

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer Bogus').first()
        item = OrderItem.query.filter_by(order_id=order.id).first()
        assert OrderItemAttachment.query.filter_by(order_item_id=item.id).count() == 0


def test_download_requires_staff(client):
    c, hinge_id = client
    res = c.post('/api/upload-order-item-pdf', data={'file': _fake_pdf()}, content_type='multipart/form-data')
    staged = res.get_json()
    cart = [{
        'type': 'detail', 'id': hinge_id, 'qty': 1,
        'attachment': {'filename': staged['filename'], 'original_filename': staged['original_filename']},
    }]
    c.post('/orders/new', data={'customer_name': 'QA Customer 2', 'cart_json': json.dumps(cart)})

    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Customer 2').first()
        item = OrderItem.query.filter_by(order_id=order.id).first()
        attachment_id = OrderItemAttachment.query.filter_by(order_item_id=item.id).first().id

    # regular_user (the one who placed the order) cannot download it back
    res = c.get(f'/order-item-files/{attachment_id}/download')
    assert res.status_code != 200

    c.get('/logout')
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    res = c.get(f'/order-item-files/{attachment_id}/download')
    assert res.status_code == 200
