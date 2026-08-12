"""
pytest regression test for api_quick_create_detail()'s optional-file
handling: an optional reference PDF (pdf_file) attached alongside the DXF,
and the DXF itself being optional - a bare-bones detail can be created with
a manually-entered price instead (see admin_details.html / the
quick-create-detail modals, and _find_or_create_delivery_target's matching
"new detail, no DXF" fallback). Uses the Flask test client (same pattern as
test_quick_create_product_components.py) since this needs real multipart
file-upload behavior.

Run with:
    pytest testing/test_detail_pdf_upload.py -v
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
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Service, Detail, DetailDxfFile, limiter


def _minimal_dxf_bytes():
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (10, 0))
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10, cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        # A Detail's cutting service must be length-priced - see
        # calculate_material_price()/_add_cutting_operation() in app.py.
        service = Service(name='QA Service', price_per_hour_eur=60, pricing_mode='length', price_per_meter_eur=5.0)
        db.session.add_all([admin, material, service])
        db.session.commit()
        yield flask_app, service.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, service_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, service_id


def test_quick_create_detail_with_pdf_attaches_file(client):
    c, service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Detail', 'material': 'qa_mat', 'service_id': str(service_id),
        'file': (io.BytesIO(_minimal_dxf_bytes()), 'part.dxf'),
        'pdf_file': (io.BytesIO(b'%PDF-1.4 fake'), 'spec.pdf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body['status'] == 'success'
    with flask_app.app_context():
        files = DetailDxfFile.query.filter_by(detail_id=body['detail']['id']).all()
        assert len(files) == 1
        assert files[0].original_filename == 'spec.pdf'


def test_quick_create_detail_without_pdf_still_works(client):
    c, service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Detail No PDF', 'material': 'qa_mat', 'service_id': str(service_id),
        'file': (io.BytesIO(_minimal_dxf_bytes()), 'part.dxf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body['status'] == 'success'
    with flask_app.app_context():
        assert DetailDxfFile.query.filter_by(detail_id=body['detail']['id']).count() == 0


def test_quick_create_detail_rejects_non_pdf_reference_file(client):
    c, service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Detail Bad PDF', 'material': 'qa_mat', 'service_id': str(service_id),
        'file': (io.BytesIO(_minimal_dxf_bytes()), 'part.dxf'),
        'pdf_file': (io.BytesIO(b'not a pdf'), 'spec.txt'),
    }, content_type='multipart/form-data')
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'
    with flask_app.app_context():
        assert Detail.query.filter_by(name='QA Detail Bad PDF').first() is None


def test_quick_create_detail_without_dxf_uses_manual_price(client):
    # No DXF at all - a bare-bones detail priced by hand, same shape
    # _find_or_create_delivery_target already creates for a delivery-note
    # "new detail" line with no DXF.
    c, _service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Manual Detail', 'material': 'qa_mat', 'manual_price': '25.50',
    }, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body['status'] == 'success'
    assert body['detail']['price'] == 25.5
    with flask_app.app_context():
        d = Detail.query.filter_by(name='QA Manual Detail').first()
        assert d is not None
        assert d.width == 0.0 and d.height == 0.0 and d.total_length == 0.0 and d.pierce_count == 0
        assert d.cutting_service_id is None
        assert d.geometry_json is None


def test_quick_create_detail_without_dxf_requires_manual_price(client):
    c, _service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA No Price Detail', 'material': 'qa_mat',
    }, content_type='multipart/form-data')
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'
    with flask_app.app_context():
        assert Detail.query.filter_by(name='QA No Price Detail').first() is None
