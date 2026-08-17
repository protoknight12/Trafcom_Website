"""
pytest regression test for storage_materials.html's stock-level row coloring:
out of stock (stock_quantity <= 0) turns the row red, at/below min_quantity
(but still > 0) turns it yellow, otherwise no highlight. Guards the Jinja
if/elif chain in templates/storage_materials.html.

Run with:
    pytest testing/test_storage_materials_thresholds.py -v
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

from app import app as flask_app, db, User, MaterialPrice, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        out_of_stock = MaterialPrice(key='qa_out', display_name='QA Изчерпан', cost_per_m2=1, stock_quantity=0, min_quantity=5)
        low_stock = MaterialPrice(key='qa_low', display_name='QA Малко', cost_per_m2=1, stock_quantity=3, min_quantity=5)
        ok_stock = MaterialPrice(key='qa_ok', display_name='QA Достатъчно', cost_per_m2=1, stock_quantity=10, min_quantity=5)
        db.session.add_all([worker, out_of_stock, low_stock, ok_stock])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def worker_client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_worker', 'password': 'irrelevant123'})
    return c


def test_stock_thresholds_color_rows(worker_client):
    res = worker_client.get('/storage/materials')
    html = res.get_data(as_text=True)

    def row_style_for(name):
        idx = html.index(name)
        row_start = html.rindex('<tr', 0, idx)
        row_end = html.index('</tr>', idx)
        return html[row_start:row_end]

    assert 'rgba(220, 53, 69' in row_style_for('QA Изчерпан')  # out of stock -> red
    assert 'rgba(255, 193, 7' in row_style_for('QA Малко')  # <= min_quantity -> yellow
    row = row_style_for('QA Достатъчно')
    assert 'rgba(220, 53, 69' not in row and 'rgba(255, 193, 7' not in row  # plenty of stock -> no highlight
