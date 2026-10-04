"""
pytest: changing a panel's room on its form moves its object on the hall plan into that room and drops a stale explicit parent,
and deleting a room shape clears every parent_id that named it.

Run with:
    pytest testing/test_hall_follow_room.py -v
"""
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Building, Room, ElectricalPanel, HallShape, HallEquipment, limiter


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _two_rooms():
    b = Building(name='B')
    db.session.add(b)
    db.session.flush()
    r1, r2 = Room(name='R1', building_id=b.id), Room(name='R2', building_id=b.id)
    db.session.add_all([r1, r2])
    db.session.flush()
    s1 = HallShape(kind='room', name='R1', room_id=r1.id, x=0, z=0, width=10, depth=10, height=3)
    s2 = HallShape(kind='room', name='R2', room_id=r2.id, x=20, z=0, width=10, depth=10, height=3)
    db.session.add_all([s1, s2])
    db.session.flush()
    return r1, r2, s1, s2


def test_panel_room_change_moves_plan_object(admin_client):
    r1, r2, s1, s2 = _two_rooms()
    p = ElectricalPanel(name='T', room_id=r1.id)
    db.session.add(p)
    db.session.flush()
    e = HallEquipment(kind='panel', ref_id=p.id, x=1, z=1, width=1, depth=1, height=1, parent_id=s1.id)
    db.session.add(e)
    db.session.commit()
    pid, eid = p.id, e.id
    admin_client.post(f'/admin/panels/{pid}/update', data={'name': 'T', 'room_id': str(r2.id)})
    db.session.expire_all()
    e = db.session.get(HallEquipment, eid)
    assert e.parent_id is None and 20 <= e.x <= 30 and 0 <= e.z <= 10


def test_room_shape_delete_clears_parents(admin_client):
    r1, r2, s1, s2 = _two_rooms()
    e = HallEquipment(kind='panel', ref_id=None, x=1, z=1, width=1, depth=1, height=1, parent_id=s1.id)
    db.session.add(e)
    db.session.commit()
    eid = e.id
    admin_client.post(f'/admin/hall/shape/{s1.id}/delete')
    db.session.expire_all()
    assert db.session.get(HallEquipment, eid).parent_id is None
