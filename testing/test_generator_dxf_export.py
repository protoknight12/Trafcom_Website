"""
pytest regression test for api_generator_dxf() (/api/generator/dxf) - the
Panel Generator's DXF export moved server-side to ezdxf's writer after the
old hand-rolled DXF text (built in templates/generator.html) was found to
skip sections/tables (BLOCK_RECORD, OBJECTS, entity handles) that real CAD
software expects, even though our own reader tolerated it fine. Guards that
the endpoint requires login, rejects bad dimensions, and produces a DXF that
round-trips through ezdxf with the expected entity count.

Run with:
    pytest testing/test_generator_dxf_export.py -v
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

from app import app as flask_app, db, User, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        user = User(username='qa_gen', password=generate_password_hash('irrelevant123'), role='regular_user')
        db.session.add(user)
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_gen', 'password': 'irrelevant123'})
    return c


def test_requires_login(app):
    res = app.test_client().post('/api/generator/dxf', json={'width': 100, 'height': 50, 'holes': []})
    assert res.status_code in (302, 401)


def test_rejects_bad_dimensions(client):
    res = client.post('/api/generator/dxf', json={'width': 'nope', 'height': 50, 'holes': []})
    assert res.status_code == 400


def test_produces_valid_dxf_with_holes(client):
    holes = [
        {'x': 30, 'y': 20, 'size': 10, 'rot': 0, 'type': 'circle'},
        {'x': 60, 'y': 20, 'size': 10, 'rot': 0.4, 'type': 'hexagon'},
        {'x': 90, 'y': 20, 'size': 10, 'rot': 0, 'type': 'hexcluster'},
    ]
    res = client.post('/api/generator/dxf', json={'width': 150, 'height': 40, 'holes': holes})
    assert res.status_code == 200
    assert res.content_type == 'application/dxf'
    assert 'Panel_150x40_Mixed.dxf' in res.headers.get('Content-Disposition', '')

    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    msp = doc.modelspace()
    # 1 border + 1 circle + 1 hexagon + 3 hexcluster rhombi = 6 entities
    assert len(msp) == 6
    assert doc.audit().errors == []
