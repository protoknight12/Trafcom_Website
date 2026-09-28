"""
pytest: admin-editable MaterialType (admin_materials.html "Типове материали").
Covers creating a type with chosen parameters + a per-piece price unit, the
pricing engine honoring it (_material_cost / _cost_per_m2_from_unit_price),
editing, and delete being refused while a material still uses the type.

Run with:
    pytest testing/test_material_types.py -v
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
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, MaterialType, limiter, seed_material_types,
                 _material_cost, _cost_per_m2_from_unit_price, material_dimension_labels)


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _fresh_types():
    g.pop('_material_types', None)  # per-context cache (see _material_types)


def test_create_per_piece_type_and_price_by_piece(admin_client):
    admin_client.post('/admin/material-types/add', data={
        'label': 'Болтове', 'price_unit': 'pcs', 'has_length': '1', 'length_label': 'Дължина на болта (мм)',
        'has_width': '1', 'width_label': '',  # checked but blank -> default label
    })
    t = MaterialType.query.filter_by(label='Болтове').one()
    assert t.key == f'type_{t.id}'
    assert t.length_label == 'Дължина на болта (мм)' and t.width_label == 'Ширина (мм)'
    assert t.thickness_label is None and t.height_label is None
    assert not t.has_cutting and not t.is_round

    _fresh_types()
    assert material_dimension_labels(t.key) == ('Дължина на болта (мм)', 'Ширина (мм)', None)
    m = MaterialPrice(key='b', display_name='Болт M8', type=t.key, cost_per_m2=0.35)
    assert _material_cost(500, 800, m) == pytest.approx(0.35), 'per-piece price ignores part size'
    assert _cost_per_m2_from_unit_price(t.key, 0.35, None, None) == 0.35

    # a saw-cut type needs no cutting speed on a new material
    admin_client.post('/admin/add_material', data={'display_name': 'Болт M8', 'type': t.key})
    assert MaterialPrice.query.filter_by(display_name='Болт M8', type=t.key).count() == 1


def test_edit_and_delete_guard(admin_client):
    t = MaterialType.query.filter_by(key='rods').one()
    admin_client.post(f'/admin/material-types/{t.id}/edit', data={
        'label': 'Кръгли пръти', 'price_unit': 'm', 'has_length': '1', 'length_label': 'Дължина (мм)',
        'has_width': '1', 'width_label': 'Диаметър (мм)', 'is_round': '1',
    })
    db.session.refresh(t)
    assert t.label == 'Кръгли пръти' and t.key == 'rods', 'key never changes - materials reference it'

    db.session.add(MaterialPrice(key='r1', display_name='Прът', type='rods', cost_per_m2=5))
    db.session.commit()
    admin_client.post(f'/admin/material-types/{t.id}/delete')
    assert MaterialType.query.get(t.id) is not None, 'type in use must not be deleted'

    other = MaterialType.query.filter_by(key='other').one()
    admin_client.post(f'/admin/material-types/{other.id}/delete')
    assert MaterialType.query.get(other.id) is None
