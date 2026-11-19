"""
pytest: the "или кг" input next to a raw-material quantity (static/js/material_weight.js).

The pages must render the kg input and the weight data attributes on the material options, and the JS
kgPerStockUnit() must give the same figure as material_kg_per_stock_unit() in app.py (cross-checked under
node, skipped when node is not installed).

Run with:
    pytest testing/test_material_kg_entry.py -v
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _safe_remove():
    try:
        os.remove(_db_path)
    except OSError:
        pass


atexit.register(_safe_remove)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import math
import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, MaterialPrice, MaterialGrade, limiter, material_kg_per_stock_unit

PASSWORD = 'irrelevant123'
JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'js', 'material_weight.js')

LOTS = {
    'sheet': dict(type='sheets', sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, density_kg_dm3=7.85),
    'rod': dict(type='rods', display_name='Прът алуминий', sheet_width_mm=20),            # density from the name
    'pipe': dict(type='pipes', sheet_width_mm=40, thickness_mm=3, density_kg_dm3=7.85),
    'profile': dict(type='profiles', sheet_width_mm=40, height_mm=60, thickness_mm=3, density_kg_dm3=7.85),
    'measured': dict(type='sheets', sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, weight_kg=30),
    'guessed': dict(type='sheets', display_name='Неръждаема ламарина', sheet_width_mm=1250, sheet_length_mm=2500,
                    thickness_mm=1.5),
    'unknown': dict(type='sheets', display_name='Нещо', sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2),
}


@pytest.fixture(scope='module')
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        found = {}
        grade = MaterialGrade(name='Тест стомана', density_kg_dm3=7.85)
        db.session.add(grade)
        db.session.flush()
        for name, kw in LOTS.items():
            kw = dict(kw)
            if kw.pop('density_kg_dm3', None):
                kw['grade_id'] = grade.id
            m = MaterialPrice(key=f'kg_{name}', **{'display_name': name, 'cost_per_m2': 10, **kw})
            db.session.add(m)
            db.session.flush()
            found[name] = m.id
        db.session.add(User(username='boss', password=generate_password_hash(PASSWORD), role='admin'))
        db.session.commit()
    yield found
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def _get(url):
    limiter.reset()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'boss', 'password': PASSWORD})
    return c.get(url).get_data(as_text=True)


def _datasets(body, ids):
    """data-* attributes of every material option of the page, by material id."""
    out = {}
    for tag in re.findall(r'<option\b[^>]*>', body):
        d = dict(re.findall(r'data-([\w-]+)="([^"]*)"', tag))
        value = re.search(r'value="([^"]*)"', tag)
        mid = d.get('id') or (value.group(1) if value else None)
        if mid and 'density' in d and 'weight' in d:
            out[str(mid)] = d
    return out


def test_pages_render_kg_input_and_option_data(ids):
    for url, input_id in (('/admin/delivery-notes', 'dn_kg'), ('/admin/client-delivery-notes', 'cdn_kg'),
                          ('/storage/requests', 'reqKg')):
        body = _get(url)
        assert f'id="{input_id}"' in body, url
        assert 'js/material_weight.js' in body, url
        data = _datasets(body, ids)
        assert str(ids['sheet']) in data and data[str(ids['sheet'])]['density'] == '7.85', url
        assert data[str(ids['measured'])]['weight'] == '30.0', url
        assert data[str(ids['unknown'])]['density'] == '', url
        assert data[str(ids['guessed'])]['density'] == '7.93', url   # guessed from the name
    assert 'id="cdn_remnant_kg"' in _get('/admin/client-delivery-notes')


@pytest.mark.parametrize('url', ['/admin/delivery-notes', '/admin/client-delivery-notes', '/storage/requests'])
def test_js_matches_server(ids, url, tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node не е инсталиран - сверката на JS със сървъра е пропусната')
    data = _datasets(_get(url), ids)
    types = {}
    with flask_app.app_context():
        from app import material_types_js
        types = material_types_js()
        expected = {name: material_kg_per_stock_unit(MaterialPrice.query.get(mid)) for name, mid in ids.items()}
    assert expected['unknown'] is None
    (tmp_path / 'in.json').write_text(json.dumps({'types': types, 'data': {n: data[str(i)] for n, i in ids.items()}}))
    (tmp_path / 'run.js').write_text(
        "const W=require(process.argv[2]);const inp=JSON.parse(require('fs').readFileSync(process.argv[3],'utf8'));"
        "const out={};for(const [n,ds] of Object.entries(inp.data)){"
        "out[n]=W.kgPerStockUnit(W.optionParams({dataset:ds},inp.types));}console.log(JSON.stringify(out));")
    run = subprocess.run([node, str(tmp_path / 'run.js'), JS, str(tmp_path / 'in.json')],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    for name, want in expected.items():
        if want is None:
            assert got[name] is None, name
        else:
            assert math.isclose(got[name], want, rel_tol=1e-9), (name, got[name], want)
