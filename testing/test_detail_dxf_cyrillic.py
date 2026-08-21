# -*- coding: utf-8 -*-
"""
pytest regression test for two bugs in the Detail DXF dashboard's revision
upload (upload_detail_dxf(), backing /details/<id>/files/upload):

1. Cyrillic (or any non-ASCII) filenames were mangled via secure_filename()
   for the DetailDxfFile.original_filename display field - werkzeug strips
   non-ASCII entirely, so an all-Cyrillic name like "панел_метален.dxf" was
   stored as just "dxf" (even the extension lost), which also broke
   get_detail_dxf_geometry()'s `.lower().endswith('.dxf')` preview gate.
   Fixed by using sanitize_display_filename() (already used by
   process_dxf_upload() for the same purpose) instead.

2. admin_add_detail()'s own docstring says a detail can be "catalogued with
   a manually-entered price up front and have its DXF/geometry added later
   via detail_dxf_dashboard()" - but upload_detail_dxf() never actually
   populated Detail.total_length/pierce_count/geometry_json, so such a
   detail's "calculate duration from DXF" helper permanently showed "Този
   детайл няма данни от DXF файл" even after a DXF was uploaded. Fixed by
   having the route fill in geometry from the upload when the detail has
   none yet (without ever overwriting existing geometry on a later upload).

Run with:
    pytest testing/test_detail_dxf_cyrillic.py -v
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

from app import app as flask_app, db, User, MaterialPrice, Detail, DetailDxfFile, limiter


def _sample_dxf_bytes(w=100.0, h=50.0):
    doc = ezdxf.new('R2000')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_cyr_admin', password=generate_password_hash('irrelevant123'), role='admin')
        db.session.add(admin)
        mat = MaterialPrice(key='qa_steel', display_name='QA Steel', cost_per_m2=10.0,
                             cutting_speed_mm_per_min=1000.0, pierce_rate_per_min=10.0)
        db.session.add(mat)
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_cyr_admin', 'password': 'irrelevant123'})
    return c


def _bare_detail():
    """A detail catalogued without a DXF - manual price, zeroed geometry,
    same shape admin_add_detail()'s no-DXF branch creates."""
    d = Detail(name='QA Деталь', material_key='qa_steel', width=0.0, height=0.0,
               total_length=0.0, pierce_count=0, calculated_price=5.0, geometry_json=None)
    db.session.add(d)
    db.session.commit()
    return d


def test_cyrillic_filename_preserved(client, app):
    with app.app_context():
        detail = _bare_detail()
        detail_id = detail.id

    data = {'file': (io.BytesIO(_sample_dxf_bytes()), 'панел_метален.dxf')}
    res = client.post(f'/details/{detail_id}/files/upload', data=data, content_type='multipart/form-data')
    assert res.status_code == 302

    with app.app_context():
        f = DetailDxfFile.query.filter_by(detail_id=detail_id).first()
        assert f.original_filename == 'панел_метален.dxf'


def test_first_dxf_upload_fills_missing_geometry(client, app):
    with app.app_context():
        detail = _bare_detail()
        detail_id = detail.id
        assert detail.total_length == 0.0

    data = {'file': (io.BytesIO(_sample_dxf_bytes(100.0, 50.0)), 'изолационна_плоча.dxf')}
    client.post(f'/details/{detail_id}/files/upload', data=data, content_type='multipart/form-data')

    with app.app_context():
        detail = db.session.get(Detail, detail_id)
        assert detail.width == 100.0
        assert detail.height == 50.0
        assert detail.total_length == 300.0  # perimeter of a 100x50 rectangle
        assert detail.geometry_json is not None


def test_second_dxf_upload_does_not_overwrite_existing_geometry(client, app):
    with app.app_context():
        detail = _bare_detail()
        detail_id = detail.id

    first = {'file': (io.BytesIO(_sample_dxf_bytes(100.0, 50.0)), 'first.dxf')}
    client.post(f'/details/{detail_id}/files/upload', data=first, content_type='multipart/form-data')

    second = {'file': (io.BytesIO(_sample_dxf_bytes(400.0, 400.0)), 'later_reference_revision.dxf')}
    client.post(f'/details/{detail_id}/files/upload', data=second, content_type='multipart/form-data')

    with app.app_context():
        detail = db.session.get(Detail, detail_id)
        assert detail.total_length == 300.0  # unchanged - still the first upload's geometry
        assert DetailDxfFile.query.filter_by(detail_id=detail_id).count() == 2
