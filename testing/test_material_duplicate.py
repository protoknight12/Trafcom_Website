"""
pytest: /admin/materials offers "Дублирай" per row - the add dialog opened in copy
mode (openMaterialDialog(btn, true)) posts to the ADD action with ERP №/КД № cleared.

Run with:
    pytest testing/test_material_duplicate.py -v
"""
from flask import g
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, limiter


def test_materials_page_has_duplicate_button_and_copy_mode():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='adm', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100,
                                     erp_number=777, code_number='КД-1'))
        db.session.commit()
        g.pop('_login_user', None)
        c = flask_app.test_client()
        c.post('/login', data={'username': 'adm', 'password': 'irrelevant123'})
        html = c.get('/admin/materials').get_data(as_text=True)
        db.session.remove()
        db.drop_all()
    assert 'openMaterialDialog(this.previousElementSibling, true)' in html
    assert html.count('>Дублирай</button>') == 1
    js = html[html.index('function openMaterialDialog(btn, copy)'):]
    assert "(btn && !copy) ? btn.dataset.action : ADD_ACTION" in js
    assert "(copy && (k === 'erp_number' || k === 'code_number')) ? null : data[k]" in js
    assert "'Нов материал (копие от '" in js
