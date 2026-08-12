"""
pytest regression test: detail_dxf_dashboard.html's operations-add form must
carry `novalidate`, and its (usually-hidden) length input must never be
pre-filled with a value below its own `min` when a Detail has no cut length
(total_length == 0, e.g. a manually-dimensioned catalog row with no DXF).

Without `novalidate`, a hidden field violating its own min="0.01" via a
stale value="0.0" makes the browser silently refuse to submit the form at
all (native constraint validation still runs on hidden fields; the browser
can't focus a display:none field to show the error, so it just aborts) -
this blocked saving ANY operation, time- or length-based, on details with no
recorded cut length. See app.py's Detail model / admin_add_operation().

Run with:
    pytest testing/test_operations_form_novalidate.py -v
"""
import atexit
import os
import re
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

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        material = MaterialPrice(key='qa_mat', display_name='QA Pipe', type='pipes', cost_per_m2=10,
                                  cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        db.session.add_all([admin, material])
        db.session.flush()
        # No DXF (geometry_json is None), total_length stays 0 - matches an
        # imported/legacy catalog row with real dimensions but no cut length.
        detail = Detail(name='QA No-Length Detail', material_key=material.key, width=50, height=200,
                         total_length=0.0, pierce_count=0, calculated_price=2.0)
        db.session.add(detail)
        db.session.commit()
        yield flask_app, detail.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    flask_app, detail_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, detail_id


def test_operations_form_has_novalidate_and_no_invalid_length_default(client):
    c, detail_id = client
    res = c.get(f'/details/{detail_id}/files')
    assert res.status_code == 200
    html = res.data.decode()

    assert '<form id="addOperationsForm"' in html
    form_tag = re.search(r'<form id="addOperationsForm"[^>]*>', html).group(0)
    assert 'novalidate' in form_tag, 'form must skip native validation on the hidden length field'

    length_input = re.search(r'<input type="number" id="op_length_mm"[^>]*>', html).group(0)
    assert 'value="0.0"' not in length_input, \
        'length input must not pre-fill with a value below its own min="0.01"'
