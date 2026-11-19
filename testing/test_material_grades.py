"""
pytest: "Вид на материала" (MaterialGrade) - add / edit / delete on admin_materials.html (delete refused while
used), the drop-down on a material (picked, or guessed from the name when left blank), the grade's density
driving the kg conversion, and the grade filter / analytics on the materials page.

Run with:
    pytest testing/test_material_grades.py -v
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
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, MaterialGrade, limiter, seed_material_types,
                 seed_material_grades, material_density, material_stock_kg)


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        seed_material_grades()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _add(c, name, **extra):
    c.post('/admin/add_material', data=dict({'display_name': name, 'type': 'sheets', 'cutting_speed_mm_per_min': '1500',
                                             'pierce_time_sec': '2', 'thickness_mm': '2', 'sheet_width_mm': '1000',
                                             'sheet_length_mm': '2000'}, **extra))
    return MaterialPrice.query.filter_by(display_name=name).one()


def test_grade_crud_and_delete_refused_while_used(admin_client):
    c = admin_client
    c.post('/admin/material-grades/add', data={'name': 'Поликарбонат', 'density_kg_dm3': '1.2'})
    pc = MaterialGrade.query.filter_by(name='Поликарбонат').one()
    assert pc.density_kg_dm3 == 1.2
    c.post('/admin/material-grades/add', data={'name': 'поликарбонат', 'density_kg_dm3': '1'})   # duplicate name
    assert MaterialGrade.query.filter(MaterialGrade.name.ilike('поликарбонат')).count() == 1
    c.post('/admin/material-grades/add', data={'name': 'Олово', 'density_kg_dm3': '99'})         # out of range
    assert not MaterialGrade.query.filter_by(name='Олово').first()

    c.post(f'/admin/material-grades/{pc.id}/edit', data={'name': 'Поликарбонат PC', 'density_kg_dm3': '1.25'})
    db.session.refresh(pc)
    assert (pc.name, pc.density_kg_dm3) == ('Поликарбонат PC', 1.25)

    m = _add(c, 'Плоскост 4мм', grade_id=str(pc.id), thickness_mm='4')
    assert m.grade_id == pc.id and material_density(m) == 1.25
    c.post(f'/admin/material-grades/{pc.id}/delete')
    assert db.session.get(MaterialGrade, pc.id) is not None                 # used -> refused
    m.grade_id = None
    db.session.commit()
    c.post(f'/admin/material-grades/{pc.id}/delete')
    assert db.session.get(MaterialGrade, pc.id) is None


def test_blank_grade_is_guessed_from_the_name_and_drives_the_kg(admin_client):
    c = admin_client
    steel = MaterialGrade.query.filter_by(name='Стомана').one()
    alu = MaterialGrade.query.filter_by(name='Алуминий').one()
    m = _add(c, 'Стомана DC01 2мм', stock_quantity='0')
    assert m.grade_id == steel.id
    m.stock_quantity = 3
    db.session.commit()
    assert material_stock_kg(m) == pytest.approx(3 * 2 * 7.85 * 2)        # 3 sheets x 2 m² x 15.7 kg/m²

    # picked by hand on edit: aluminium's density from now on
    c.post(f'/admin/update_material/{m.key}', data={'display_name': m.display_name, 'type': 'sheets', 'grade_id': str(alu.id),
                                                    'cutting_speed_mm_per_min': '1500', 'pierce_time_sec': '2',
                                                    'thickness_mm': '2', 'sheet_width_mm': '1000', 'sheet_length_mm': '2000'})
    db.session.refresh(m)
    assert m.grade_id == alu.id and material_stock_kg(m) == pytest.approx(3 * 2 * 2.70 * 2)

    page = c.get('/admin/materials').get_data(as_text=True)
    assert 'id="filterGrade"' in page and 'id="summaryGroup"' in page and 'Видове материали' in page
    assert f'data-grade="{alu.id}"' in page and 'name="grade_id"' in page
