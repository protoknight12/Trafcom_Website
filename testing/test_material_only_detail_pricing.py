"""
pytest regression test for the material-only Detail pricing refactor:
Detail.calculated_price is now material cost + BASE_SETUP_FEE only (see
calculate_material_price()); cutting cost is instead captured as an
auto-attached length-priced Operation (_add_cutting_operation()) at
DXF-upload time, so Detail.total_price still equals material + cutting (+ any
other operations). Also guards that a Detail's cutting service must be
length-priced (pricing_mode == 'length'), and that changing material via
admin_update_detail_material() only touches calculated_price, not the
cutting Operation. Uses the Flask test client (same pattern as
test_detail_pdf_upload.py) since this needs real multipart file-upload
behavior.

Run with:
    pytest testing/test_material_only_detail_pricing.py -v
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

from app import app as flask_app, db, User, MaterialPrice, Service, Detail, Operation, limiter


def _minimal_dxf_bytes(length_mm=10):
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (length_mm, 0))
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
        # area = 0 for a bare line (height stays 0), so material_cost is 0 and
        # calculated_price collapses to just BASE_SETUP_FEE - keeps the math
        # in this test independent of BASE_SETUP_FEE's actual value.
        material = MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10,
                                  cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        length_service = Service(name='Лазер по дължина', price_per_hour_eur=999,
                                  pricing_mode='length', price_per_meter_eur=5.0)  # 0.005 EUR/mm
        time_service = Service(name='Стар часови лазер', price_per_hour_eur=60)  # pricing_mode default 'time'
        db.session.add_all([admin, material, length_service, time_service])
        db.session.commit()
        yield flask_app, length_service.id, time_service.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, length_service_id, time_service_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, length_service_id, time_service_id


def test_calculated_price_is_material_only_and_cutting_becomes_an_operation(client):
    c, length_service_id, _time_service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Detail', 'material': 'qa_mat', 'service_id': str(length_service_id),
        'file': (io.BytesIO(_minimal_dxf_bytes(10)), 'part.dxf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    detail_id = res.get_json()['detail']['id']

    with flask_app.app_context():
        detail = db.session.get(Detail, detail_id)
        from app import BASE_SETUP_FEE
        assert detail.calculated_price == round(BASE_SETUP_FEE, 2)  # material_cost is 0 (zero-height line)

        ops = Operation.query.filter_by(detail_id=detail.id).all()
        assert len(ops) == 1
        cutting_op = ops[0]
        assert cutting_op.service_id == length_service_id
        assert cutting_op.length_mm == 10.0
        assert cutting_op.cost == round(10 / 1000 * 5.0, 2)  # 0.05

        assert detail.total_price == round(detail.calculated_price + cutting_op.cost, 2)


def test_time_priced_service_rejected_as_cutting_service(client):
    c, _length_service_id, time_service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Bad Detail', 'material': 'qa_mat', 'service_id': str(time_service_id),
        'file': (io.BytesIO(_minimal_dxf_bytes(10)), 'part.dxf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'
    with flask_app.app_context():
        assert Detail.query.filter_by(name='QA Bad Detail').first() is None


def test_update_material_recomputes_material_only_price_without_touching_cutting_op(client):
    c, length_service_id, _time_service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Detail 2', 'material': 'qa_mat', 'service_id': str(length_service_id),
        'file': (io.BytesIO(_minimal_dxf_bytes(10)), 'part.dxf'),
    }, content_type='multipart/form-data')
    detail_id = res.get_json()['detail']['id']

    c.post(f'/details/{detail_id}/update-material', data={
        'material': 'qa_mat', 'width': '1000', 'height': '500',  # 0.5 m^2 * 10 EUR/m^2 = 5.0
    })

    with flask_app.app_context():
        from app import BASE_SETUP_FEE
        detail = db.session.get(Detail, detail_id)
        assert detail.calculated_price == round(5.0 + BASE_SETUP_FEE, 2)
        # The cutting Operation from creation time is untouched by the material update.
        ops = Operation.query.filter_by(detail_id=detail.id).all()
        assert len(ops) == 1
        assert ops[0].length_mm == 10.0
