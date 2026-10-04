"""
pytest: /admin/machines/merge - two duplicate machine rows become one (orders, services, dossier files and identification move over).

Run with:
    pytest testing/test_machine_merge.py -v
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

from app import app as flask_app, db, User, Machine, HallMachine, HallMachineFile, MachineConnection, Order, Service, limiter


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


def test_merge_dossier_and_machine(admin_client):
    a, b = Machine(name='Fiber'), Machine(name='Fiber 2', room_id=None, machine_type='laser')
    svc = Service(name='cut', machine_type='laser', price_per_hour_eur=10)
    ha = HallMachine(name='Fiber', machine=a, serial_number='')
    hb = HallMachine(name='Fiber 2', machine=b, serial_number='SN1', notes='n')
    db.session.add_all([a, b, svc, ha, hb])
    db.session.flush()
    b.services.append(svc)
    db.session.add_all([Order(order_number='T-1', user_id=1, customer_name='x', machine_id=b.id), MachineConnection(hall_machine_id=hb.id),
                        HallMachineFile(machine_id=hb.id, original_name='f.txt', stored_name='nofile.txt')])
    db.session.commit()
    ha_id, hb_id, a_id, b_id = ha.id, hb.id, a.id, b.id
    r = admin_client.post('/admin/machines/merge', data={'keep': f'hm:{ha_id}', 'drop': f'hm:{hb_id}'})
    assert r.status_code == 302
    db.session.expire_all()
    assert db.session.get(HallMachine, hb_id) is None and db.session.get(Machine, b_id) is None
    keep, m = db.session.get(HallMachine, ha_id), db.session.get(Machine, a_id)
    assert keep.name == 'Fiber' and keep.serial_number == 'SN1' and keep.notes == 'n' and keep.machine_id == a_id
    assert len(keep.files) == 1 and len(keep.connections) == 1
    assert [s.name for s in m.services] == ['cut'] and m.machine_type == 'laser'
    assert Order.query.filter_by(machine_id=a_id).count() == 1


def test_merge_into_machine_without_dossier(admin_client):
    a, b = Machine(name='Plain'), Machine(name='Dup')
    hb = HallMachine(name='Dup', machine=b)
    db.session.add_all([a, b, hb])
    db.session.commit()
    a_id, hb_id = a.id, hb.id
    admin_client.post('/admin/machines/merge', data={'keep': f'm:{a_id}', 'drop': f'hm:{hb_id}'})
    db.session.expire_all()
    h = db.session.get(HallMachine, hb_id)
    assert h.machine_id == a_id and h.name == 'Plain' and Machine.query.count() == 1


def test_merge_same_row_refused(admin_client):
    a = Machine(name='One')
    db.session.add(a)
    db.session.commit()
    admin_client.post('/admin/machines/merge', data={'keep': f'm:{a.id}', 'drop': f'm:{a.id}'})
    assert Machine.query.count() == 1
