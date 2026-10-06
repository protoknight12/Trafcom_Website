"""Own-objects tool: classes, a training image with boxes, and the COCO export (pytest, throwaway sqlite)."""
import io
import json
import os
import tempfile
import zipfile

_fd, _path = tempfile.mkstemp(suffix='.db')
os.close(_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_path}'

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

from app import app, db, User, CustomClass, TrainImage


@pytest.fixture
def client():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        u = User.query.filter_by(username='adm').first()
        if u is None:
            u = User(username='adm', password=generate_password_hash('Passw0rd!x'), role='admin')
            db.session.add(u)
            db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return c


def test_label_and_export(client):
    client.post('/admin/objects/classes', data={'name': 'палет'})
    client.post('/admin/objects/classes', data={'name': 'мотокар'})
    jpg = io.BytesIO()
    Image.new('RGB', (200, 100), 'gray').save(jpg, 'JPEG')
    jpg.seek(0)
    client.post('/admin/objects/images/upload', data={'files': (jpg, 'a.jpg')}, content_type='multipart/form-data')
    with app.app_context():
        cid = CustomClass.query.filter_by(name='мотокар').one().id
        iid = TrainImage.query.one().id
    r = client.post(f'/admin/objects/images/{iid}/boxes', json={'boxes': [{'cls': cid, 'box': [0.5, 0.25, 0.75, 0.5]}, {'cls': 9999, 'box': [0, 0, 1, 1]}]})
    assert r.get_json()['n'] == 1                                  # the unknown class is dropped
    z = zipfile.ZipFile(io.BytesIO(client.get('/admin/objects/export.zip').data))
    coco = json.loads(z.read('annotations.json'))
    assert coco['annotations'][0]['bbox'] == [100.0, 25.0, 50.0, 25.0] and coco['annotations'][0]['category_id'] == 2
    assert json.loads(z.read('custom.json')) == ['палет', 'мотокар'] and f'images/{iid}.jpg' in z.namelist()
