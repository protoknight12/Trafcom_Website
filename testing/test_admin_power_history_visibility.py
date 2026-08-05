"""
pytest regression test: admin_power()'s "История за период" section should
only render when there's actually a chance of getting something back -
always for a Gen2 host (shelly_history() can always be asked, even if the
answer is empty), and for a Gen1 host only once ShellyReadingLog has at
least one row logged for it (see _aggregate_local_shelly_log()). A Gen1 host
with nothing logged yet would just be a form that always comes back empty,
so admin_power() skips rendering it via the focus_history_available flag.

Run with:
    pytest testing/test_admin_power_history_visibility.py -v
"""
import atexit
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

import pytest
from werkzeug.security import generate_password_hash

import app as appmod
from app import app as flask_app, db, User, ShellyDevice, ShellyReadingLog, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        db.session.add(admin)
        db.session.add_all([
            ShellyDevice(name='QA Gen2 meter', host='10.0.0.1'),
            ShellyDevice(name='QA Gen1 meter, no log', host='10.0.0.2'),
            ShellyDevice(name='QA Gen1 meter, with log', host='10.0.0.3'),
        ])
        db.session.add(ShellyReadingLog(host='10.0.0.3', ts=1000, total_power=1.0, total_energy=1.0))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()
        appmod._shelly_gen_cache.clear()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def test_gen2_host_always_shows_the_history_section(app, admin_client):
    resp = admin_client.get('/admin/power?host=10.0.0.1')
    assert b'id="historySection"' in resp.data


def test_gen1_host_with_no_logged_rows_hides_the_history_section(app, admin_client):
    appmod._shelly_gen_cache['10.0.0.2'] = 'gen1'
    resp = admin_client.get('/admin/power?host=10.0.0.2')
    assert b'id="historySection"' not in resp.data


def test_gen1_host_with_logged_rows_shows_the_history_section(app, admin_client):
    appmod._shelly_gen_cache['10.0.0.3'] = 'gen1'
    resp = admin_client.get('/admin/power?host=10.0.0.3')
    assert b'id="historySection"' in resp.data
