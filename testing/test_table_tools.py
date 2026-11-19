"""
pytest: search + column filters on every list table (static/js/table_tools.js, owner's decision 2026-11-19).
Checks that the script is loaded on list pages BEFORE remember_filters.js (both run on DOMContentLoaded in
registration order - the controls must exist when the saved filter values are restored), that a list page still
renders its table in the shape the script looks for, and that the script parses under node (skipped without node).
The behaviour itself (which tables get a bar, filters, restore after leaving the page) was checked in a browser.

Run with:
    python -m pytest testing/test_table_tools.py -q -p no:cacheprovider
"""
import os
import shutil
import subprocess

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, limiter, User, MaterialPrice, MaterialRequest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'static', 'js', 'table_tools.js')
PAGES = ['/storage/requests', '/admin/offers', '/admin/production-orders', '/admin/users', '/admin/log',
         '/admin/services', '/orders', '/dashboard']


@pytest.fixture(scope='module')
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='tt_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        mat = MaterialPrice(key='tt_sheet', display_name='TT Ламарина', type='sheets', cost_per_m2=10,
                            cutting_speed_mm_per_min=1, pierce_rate_per_min=0.1)
        db.session.add(mat)
        db.session.flush()
        for i in range(7):
            db.session.add(MaterialRequest(material_id=mat.id, quantity=i + 1, status=['new', 'ordered'][i % 2]))
        db.session.commit()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'tt_admin', 'password': 'irrelevant123'})
    return c


@pytest.mark.parametrize('url', PAGES)
def test_script_loaded_before_remember_filters(client, url):
    html = client.get(url).get_data(as_text=True)
    assert 'js/table_tools.js' in html, url
    assert html.index('js/table_tools.js') < html.index('js/remember_filters.js'), url


def test_request_list_has_enhanceable_table(client):
    html = client.get('/storage/requests').get_data(as_text=True)
    assert '<table class="data-table">' in html and '<thead>' in html
    assert html.count('TT Ламарина') >= 7


def test_script_parses_under_node():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    res = subprocess.run([node, '--check', SCRIPT], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
