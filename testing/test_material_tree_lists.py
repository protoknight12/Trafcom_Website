"""
pytest: Склад/Материали and Материали pages render the Тип -> Вид -> материал -> партида tree
(tree-head rows, data-parent links) and the storage page carries the client-side filters.

Run with:  python -m pytest testing/test_material_tree_lists.py -q
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
atexit.register(lambda: os.path.exists(_db_path) and _try_remove())


def _try_remove():
    try:
        os.remove(_db_path)
    except OSError:
        pass


os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, MaterialGrade, limiter


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='tree_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        steel = MaterialGrade(name='Стомана ТЕСТ', density_kg_dm3=7.85)
        db.session.add(steel)
        db.session.flush()
        db.session.add_all([
            MaterialPrice(key='tree_a', display_name='Лист А', type='sheets', cost_per_m2=5, stock_quantity=3,
                          sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, grade_id=steel.id),
            MaterialPrice(key='tree_b', display_name='Лист А', type='sheets', cost_per_m2=6, stock_quantity=2,
                          sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, grade_id=steel.id),
            MaterialPrice(key='tree_c', display_name='Лист Б', type='sheets', cost_per_m2=4, stock_quantity=1,
                          sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=3),
        ])
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'tree_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def test_storage_tree(admin_client):
    html = admin_client.get('/storage/materials').get_data(as_text=True)
    assert 'js/tree_table.js' in html
    assert 'data-node="t-sheets"' in html            # Тип
    assert 'data-node="g-sheets-none"' in html       # Без вид
    assert 'Стомана ТЕСТ' in html and 'data-parent="t-sheets"' in html
    assert 'data-parent="g-sheets-' in html          # material / leaf under the grade
    assert 'data-parent="m-1"' in html               # the two price lots of Лист А under their group header
    for el in ('filterType', 'filterGrade', 'filterBrand', 'filterThickness', 'filterStatus', 'ownerFilter',
               'materialSearchInput'):
        assert 'id="%s"' % el in html
    assert 'data-tree-all="expand"' in html


def test_admin_materials_tree(admin_client):
    html = admin_client.get('/admin/materials').get_data(as_text=True)
    assert 'js/tree_table.js' in html
    assert 'data-tree-section="box-sheets"' in html
    assert 'class="tree-head tree-l1" data-node="g-sheets-none"' in html
    assert 'Стомана ТЕСТ' in html
    assert html.count('data-parent="g-sheets-') >= 3   # every material row sits under a grade header
    assert 'id="filterGrade"' in html and 'data-grade-name=' in html
