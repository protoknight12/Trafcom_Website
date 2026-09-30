"""
pytest: /flashing - the sheet-metal profile unfold (_flashing_unfold), its DXF
(layers, units, bend lines never priced as cuts) and the preset endpoints.

Run with:
    pytest testing/test_flashing.py -v
"""
import atexit
import io
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps it locked while SQLAlchemy's pool is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import ezdxf
import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, limiter, _flashing_unfold, analyze_dxf_geometry

BASE = {'profile': 'L', 'thickness': 2, 'radius': 2, 'k_factor': 0.4, 'length': 1000,
        'flanges': [50, 30], 'angles': [90]}


def test_l_profile_flat_length_matches_hand_calc():
    u = _flashing_unfold(BASE)
    # setback (2+2)*tan45 = 4, BA = pi/2*(2+0.4*2) = 4.398, BD = 8 - 4.398
    assert u['bends'][0]['allowance'] == pytest.approx(4.398, abs=1e-3)
    assert u['bends'][0]['deduction'] == pytest.approx(3.602, abs=1e-3)
    assert u['flat_length'] == pytest.approx(80 - 3.602, abs=2e-3)
    assert u['bends'][0]['line'] == pytest.approx(46 + 4.398 / 2, abs=1e-3)


def test_profiles_bend_counts_and_directions():
    z = _flashing_unfold({**BASE, 'profile': 'Z', 'flanges': [20, 40, 20], 'angles': [90, 90]})
    assert [b['dir'] for b in z['bends']] == ['up', 'down']
    hat = _flashing_unfold({**BASE, 'profile': 'HAT', 'flanges': [20, 30, 60, 30, 20], 'angles': [90] * 4})
    assert len(hat['bends']) == 4 and len(hat['flanges']) == 5
    assert hat['profile'] == [(0, 0), (20, 0), (20, 30), (80, 30), (80, 0), (100, 0)]


def test_hems_add_flange_and_180_bend():
    u = _flashing_unfold({**BASE, 'hem_start': True, 'hem_end': True, 'hem_length': 10})
    assert len(u['flanges']) == 4 and [b['angle'] for b in u['bends']] == [180, 90, 180]
    assert [b['kind'] for b in u['bends']] == ['hem', 'bend', 'hem']
    assert [f['n'] for f in u['flanges']] == ['1п', '1', '2', '2п']  # hems belong to their side


def test_hem_angle_is_configurable():
    u = _flashing_unfold({**BASE, 'hem_end': True, 'hem_length': 10, 'hem_angle': 45})
    assert u['bends'][-1]['angle'] == 45 and u['bends'][-1]['kind'] == 'hem'
    with pytest.raises(ValueError):
        _flashing_unfold({**BASE, 'hem_end': True, 'hem_length': 10, 'hem_angle': 200})


def test_side_keeps_its_size_with_or_without_hem():
    # Farthest point of the side (along its axis) = its straight part + fold apex (r+t); 50 stays 50.
    plain = _flashing_unfold(BASE)
    hem = _flashing_unfold({**BASE, 'hem_end': True, 'hem_length': 8, 'hem_angle': 180})
    side = hem['flanges'][1]  # side 2 (30) carries the end hem; side 1 unaffected
    assert side['n'] == '2'
    assert hem['flanges'][0]['straight'] == plain['flanges'][0]['straight']
    assert side['straight'] == pytest.approx(30 - 4 - 2, abs=1e-6)  # minus bend setback (r+t=4), minus hem apex (hem r 0 + t=2)
    opened = _flashing_unfold({**BASE, 'hem_end': True, 'hem_length': 8, 'hem_angle': 45})
    assert opened['flanges'][1]['straight'] > 0


def test_tapered_part_has_slanted_bend_lines_and_trapezoid_contour(client, tmp_path):
    tp = {**BASE, 'flanges_end': [50, 60], 'relief': True, 'relief_width': 3, 'relief_depth': 4,
          'holes': [{'flange': 2, 'u': 5, 'y': 500, 'kind': 'circle', 'd': 4}]}
    u = _flashing_unfold(tp)
    assert u['tapered'] and u['flat_length_end'] == pytest.approx(u['flat_length'] + 30, abs=1e-6)
    b = u['bends'][0]
    assert b['line_end'] == b['line']  # side 1 unchanged, so the bend line stays put...
    u2 = _flashing_unfold({**BASE, 'flanges': [50, 30], 'flanges_end': [70, 30]})
    assert u2['bends'][0]['line_end'] == pytest.approx(u2['bends'][0]['line'] + 20, abs=1e-6)  # ...but here it slants
    # hole at mid-length sits half-way between the two ends of its side
    assert u['holes'][0]['cx'] == pytest.approx(u['flanges'][1]['x0'] + 5, abs=1e-6)
    res = client.post('/api/flashing/dxf', json=tp)
    assert res.status_code == 200 and 'tapered' not in res.get_data(as_text=True)
    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    line = [e for e in doc.modelspace() if e.dxftype() == 'LINE'][0]
    assert line.dxf.end.y == 1000


def test_inside_dimensions_convert_to_outside():
    out = _flashing_unfold(BASE)
    ins = _flashing_unfold({**BASE, 'dim_mode': 'inside', 'flanges': [50 - 2, 30 - 2]})  # t=2, 90° -> +t per bend
    assert [f['outside'] for f in ins['flanges']] == [50, 30] and ins['flat_length'] == out['flat_length']
    assert ins['flanges'][0]['given'] == 48


def test_relief_and_holes_shape_the_contour():
    u = _flashing_unfold({**BASE, 'relief': True, 'relief_width': 3, 'relief_depth': 4,
                          'holes': [{'flange': 1, 'u': 20, 'y': 100, 'kind': 'circle', 'd': 6},
                                    {'flange': 2, 'u': 10, 'y': 200, 'kind': 'rect', 'w': 4, 'h': 8}]})
    assert len(u['contour']) == 4 + 8 * 1  # 4 corners + one 4-pt notch per end
    assert len(u['holes']) == 2 and u['warnings'] == []


def test_hole_in_bend_zone_warns_and_outside_errors():
    u = _flashing_unfold({**BASE, 'holes': [{'flange': 1, 'u': 45, 'y': 100, 'kind': 'circle', 'd': 6}]})
    assert u['warnings']
    with pytest.raises(ValueError):
        _flashing_unfold({**BASE, 'holes': [{'flange': 1, 'u': 500, 'y': 100, 'kind': 'circle', 'd': 6}]})


@pytest.mark.parametrize('patch', [
    {'thickness': 0}, {'flanges': [50]}, {'angles': [180]}, {'profile': 'X'},
    {'flanges': [3, 30]},  # flange shorter than its own setback
])
def test_bad_input_rejected(patch):
    with pytest.raises(ValueError):
        _flashing_unfold({**BASE, **patch})


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_fl', password=generate_password_hash('irrelevant123'), role='regular_user'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_fl', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def test_dxf_layers_units_and_pricing_ignores_bend_lines(client, tmp_path):
    res = client.post('/api/flashing/dxf', json={**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90]})
    assert res.status_code == 200
    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    assert doc.header.get('$INSUNITS') == 4
    layers = sorted(e.dxf.layer for e in doc.modelspace())
    assert layers == ['BEND_UP', 'BEND_UP', 'CUT']
    path = tmp_path / 'f.dxf'
    path.write_bytes(res.data)
    w, h, total, pierces, _ = analyze_dxf_geometry(str(path))
    assert total == pytest.approx(2 * (w + h), abs=0.01) and pierces == 1  # contour only


def test_calc_endpoint_error_and_presets(client):
    assert client.post('/api/flashing/calc', json={**BASE, 'thickness': -1}).status_code == 400
    assert client.post('/api/flashing/calc', json=BASE).get_json()['flat_length'] > 0
    assert client.post('/api/flashing-presets', data={'name': 'a', 'settings_json': '{"x":1}'}).status_code == 200
    client.post('/api/flashing-presets', data={'name': 'a', 'settings_json': '{"x":2}'})  # overwrite
    presets = client.get('/api/flashing-presets').get_json()['presets']
    assert len(presets) == 1 and presets[0]['settings'] == {'x': 2}
    assert client.post(f"/api/flashing-presets/{presets[0]['id']}/delete").status_code == 200
    assert client.get('/api/flashing-presets').get_json()['presets'] == []
