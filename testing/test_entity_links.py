"""entity_url() / elink(): a clicked name leads to the right page for each role, plain text otherwise."""
import pytest
from flask import render_template_string

from werkzeug.security import generate_password_hash

from app import app, db, User, MaterialPrice, entity_url, seed_material_types


@pytest.fixture()
def ctx():
    app.config['TESTING'] = True
    with app.app_context():
        db.create_all()
        seed_material_types()
        yield


def _login(client, user):
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True


def _user(role, name):
    u = User.query.filter_by(username=name).first()
    if not u:
        u = User(username=name, role=role, password=generate_password_hash('irrelevant123'))
        db.session.add(u)
        db.session.commit()
    return u


def _urls(role, name):
    u = _user(role, name)
    out = {}
    with app.test_request_context('/'):
        from flask_login import login_user
        login_user(u)
        for kind in ('detail', 'product', 'material', 'order', 'offer'):
            out[kind] = entity_url(kind, 7)
    return out


def test_admin_gets_every_page(ctx):
    u = _urls('admin', 'el_admin')
    assert u['detail'] == '/details/7/files'
    assert u['product'] == '/admin/products/7/edit'
    assert u['material'].endswith('dialog=edit%3A7') or u['material'].endswith('dialog=edit:7')
    assert u['order'] == '/admin/orders/7/edit'
    assert u['offer'] == '/admin/offers/7/edit'


def test_worker_sees_production_anchor_only_for_orders(ctx):
    u = _urls('worker', 'el_worker')
    assert u['detail'] == '/details/7/files'
    assert u['order'].endswith('/admin/production#order-7')
    assert u['product'] == u['material'] == u['offer'] == ''


def test_customer_only_detail(ctx):
    u = _urls('regular_user', 'el_user')
    assert u['detail'] == '/details/7/files'
    assert u['order'] == u['product'] == u['material'] == u['offer'] == ''


def test_anonymous_and_missing_id_give_plain_text(ctx):
    with app.test_request_context('/'):
        assert entity_url('detail', 7) == ''
        out = render_template_string(
            "{% from 'partials/entity_link.html' import elink %}[{{ elink('detail', 7, '<b>x</b>') }}]")
        assert out == '[&lt;b&gt;x&lt;/b&gt;]'


def test_macro_renders_link_and_frame_attributes(ctx):
    u = _user('admin', 'el_admin')
    with app.test_request_context('/'):
        from flask_login import login_user
        login_user(u)
        tpl = ("{% from 'partials/entity_link.html' import elink %}"
               "{{ elink('detail', 3, 'Капак') }}|{{ elink('offer', 4, 'О-4') }}|{{ elink('order', 5, 'ORD') }}")
        out = render_template_string(tpl)
    assert '<a href="/details/3/files" class="entity-link">Капак</a>' in out
    assert 'data-frame data-frame-scope="/admin/offers/"' in out
    assert 'data-frame="stay"' in out


def test_material_edit_dialog_target_exists_on_page(ctx):
    """The materials page carries data-edit-id on its edit button so ?dialog=edit:ID can click it."""
    u = _user('admin', 'el_admin')
    m = MaterialPrice(key='el_mat', display_name='Тест листа', type='sheets', cost_per_m2=0)
    db.session.add(m)
    db.session.commit()
    c = app.test_client()
    _login(c, u)
    html = c.get('/admin/materials').get_data(as_text=True)
    assert f'data-edit-id="{m.id}"' in html
