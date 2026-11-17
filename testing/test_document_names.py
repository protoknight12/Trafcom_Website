"""
pytest: documents print people by name - User.full_name ("Име Фамилия", username when empty), set by an admin on
/admin/users or by the user on /account; Съставил on the delivery-note prints; Предал/Приел on the protocols default
to the author and the client's МОЛ (_note_signatures()), an admin changes and saves them, a worker only sees them.

Run with:
    pytest testing/test_document_names.py -v
"""
import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, DeliveryNote, ClientDeliveryNote, limiter, _note_signatures)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin',
                     first_name='Иван', last_name='Петров')
        worker = User(username='qa_worker', password=generate_password_hash('irrelevant123'), role='worker')
        client = Client(name='АРМИКО ООД', client_type='company', mol='Мария Георгиева')
        db.session.add_all([admin, worker, client])
        db.session.flush()
        issue = ClientDeliveryNote(client_id=client.id, kind='issue', created_by_id=admin.id)
        receipt = DeliveryNote(client_id=client.id, created_by_id=admin.id)
        db.session.add_all([issue, receipt])
        db.session.commit()
        yield dict(admin=admin.id, worker=worker.id, issue=issue.id, receipt=receipt.id)
        db.session.remove()
        db.drop_all()


def _login(username):
    """The fixture keeps one app context open, so flask-login's cached user (g) would leak between clients."""
    from flask import g
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_full_name_falls_back_to_username():
    assert User(username='x', first_name='Иван', last_name='Петров').full_name == 'Иван Петров'
    assert User(username='x', first_name='Иван').full_name == 'Иван'
    assert User(username='x').full_name == 'x'


def test_default_signatures(ids):
    with flask_app.app_context():
        assert _note_signatures(db.session.get(ClientDeliveryNote, ids['issue'])) == ('Иван Петров', 'Мария Георгиева')
        assert _note_signatures(db.session.get(DeliveryNote, ids['receipt'])) == ('Мария Георгиева', 'Иван Петров')


def test_prints_show_names_not_usernames(ids):
    c = _login('qa_admin')
    for url in (f"/admin/client-delivery-notes/{ids['issue']}/print", f"/admin/delivery-notes/{ids['receipt']}/print"):
        html = c.get(url).get_data(as_text=True)
        assert 'Съставил: Иван Петров' in html and 'value="Мария Георгиева"' in html and 'value="Иван Петров"' in html
        assert 'Запази имената' in html


def test_admin_saves_and_resets_signatures(ids):
    c = _login('qa_admin')
    res = c.post(f"/admin/client-delivery-notes/{ids['issue']}/signatures",
                 data={'handed_by': 'Георги Иванов', 'received_by': 'Мария Георгиева'})
    assert res.status_code == 302
    with flask_app.app_context():
        note = db.session.get(ClientDeliveryNote, ids['issue'])
        assert (note.handed_by, note.received_by) == ('Георги Иванов', None)   # same as the default -> not stored
        assert _note_signatures(note) == ('Георги Иванов', 'Мария Георгиева')
    c.post(f"/admin/client-delivery-notes/{ids['issue']}/signatures", data={'handed_by': '', 'received_by': ''})
    with flask_app.app_context():
        assert _note_signatures(db.session.get(ClientDeliveryNote, ids['issue'])) == ('Иван Петров', 'Мария Георгиева')
    c.post(f"/admin/delivery-notes/{ids['receipt']}/signatures", data={'handed_by': 'Шофьор', 'received_by': ''})
    with flask_app.app_context():
        assert _note_signatures(db.session.get(DeliveryNote, ids['receipt'])) == ('Шофьор', 'Иван Петров')


def test_worker_sees_names_read_only(ids):
    c = _login('qa_worker')
    html = c.get(f"/admin/client-delivery-notes/{ids['issue']}/print").get_data(as_text=True)
    assert 'readonly' in html and 'Запази имената' not in html
    c.post(f"/admin/client-delivery-notes/{ids['issue']}/signatures", data={'handed_by': 'X', 'received_by': 'Y'})
    with flask_app.app_context():
        assert db.session.get(ClientDeliveryNote, ids['issue']).handed_by is None


def test_names_set_by_admin_and_by_the_user(ids):
    c = _login('qa_admin')
    c.post(f"/admin/users/{ids['worker']}/update", data={'role': 'worker', 'client_id': '',
                                                         'first_name': ' Петър ', 'last_name': 'Стоянов'})
    html = c.get('/admin/users').get_data(as_text=True)
    assert 'Петър Стоянов' in html
    w = _login('qa_worker')
    from flask import g
    g.pop('_login_user', None)
    w.post('/account/name', data={'first_name': 'Петър', 'last_name': ''})
    with flask_app.app_context():
        assert db.session.get(User, ids['worker']).full_name == 'Петър'


def test_user_cannot_take_a_colleagues_name(ids):
    """Self-service refuses a name another account shows (full name or username) - no signing as a colleague."""
    from flask import g
    g.pop('_login_user', None)
    w = _login('qa_worker')
    g.pop('_login_user', None)
    w.post('/account/name', data={'first_name': 'иван', 'last_name': 'ПЕТРОВ'})
    g.pop('_login_user', None)
    w.post('/account/name', data={'first_name': 'qa_admin', 'last_name': ''})
    with flask_app.app_context():
        assert db.session.get(User, ids['worker']).full_name == 'qa_worker'
