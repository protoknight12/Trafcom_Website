"""
pytest: the secondary navigation bar (partials/subnav.html) - "Назад" + the section's
quick links sit under the main navbar, not in it; a record page shows its name as a
crumb; workers see only the non-admin links; product_edit uses the shared navbar.

Run with:
    pytest testing/test_subnav.py -v
"""
import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, Detail, Product, limiter


@pytest.fixture
def setup():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([User(username='adm', password=pw, role='admin'),
                            User(username='wrk', password=pw, role='worker'),
                            MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100)])
        d = Detail(name='Пластина X', material_key='steel', width=1, height=1, total_length=0, pierce_count=0, calculated_price=0)
        p = Product(name='Кутия Y', markup_percent=0)
        db.session.add_all([d, p])
        db.session.commit()
        yield dict(detail=d, product=p)
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_catalog_list_has_subnav_below_navbar(setup):
    html = _login('adm').get('/admin/details').get_data(as_text=True)
    assert 'nav-admin-back' not in html                      # "Назад" left the main menu
    assert html.index('</nav>') < html.index('class="subnav"')
    assert 'class="subnav-back"' in html
    assert '<a href="/admin/details" class="active">Детайли</a>' in html
    assert '<a href="/admin/products" class="">Продукти</a>' in html
    assert 'subnav-current' not in html


def test_record_pages_show_crumb_and_shared_navbar(setup):
    adm = _login('adm')
    html = adm.get(f'/details/{setup["detail"].id}/files').get_data(as_text=True)
    assert '<span class="subnav-current">Пластина X</span>' in html
    html = adm.get(f'/admin/products/{setup["product"].id}/edit').get_data(as_text=True)
    assert 'id="navLinks"' in html                           # the shared navbar, not the old private one
    assert '<span class="subnav-current">Кутия Y</span>' in html
    assert html.count('js/form_dialog.js') == 1              # loaded once (navbar), never twice
    assert 'Обратно към Продукти' not in html


def test_worker_sees_only_staff_links(setup):
    html = _login('wrk').get('/storage/materials').get_data(as_text=True)
    assert 'class="subnav"' in html
    assert 'Склад материали</a>' in html and 'Доставки</a>' in html
    assert '>Детайли</a>' not in html and '>Материали</a>' not in html


def test_back_prefers_same_host_referrer(setup):
    adm = _login('adm')
    html = adm.get('/admin/details', headers={'Referer': 'http://localhost/admin/products'}).get_data(as_text=True)
    assert 'href="http://localhost/admin/products" class="subnav-back"' in html
    html = adm.get('/admin/details', headers={'Referer': 'http://evil.example/x'}).get_data(as_text=True)
    assert 'href="/admin/details" class="subnav-back"' in html   # falls back to the section's first page
