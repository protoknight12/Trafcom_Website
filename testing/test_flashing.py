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
    assert line.dxf.end.y == 996  # trimmed by the far-end relief notch (depth 4)


def test_inside_dimensions_convert_to_outside():
    out = _flashing_unfold(BASE)
    ins = _flashing_unfold({**BASE, 'dim_mode': 'inside', 'flanges': [50 - 2, 30 - 2]})  # t=2, 90° -> +t per bend
    assert [f['outside'] for f in ins['flanges']] == [50, 30] and ins['flat_length'] == out['flat_length']
    assert ins['flanges'][0]['given'] == 48


def test_corner_cuts_shape_the_contour():
    rect = {'kind': 'rect', 'w': 5, 'h': 8}
    u = _flashing_unfold({**BASE, 'corners': [rect] * 4})
    assert len(u['contour']) == 12 and (0.0, 8.0) in u['contour'] and (5.0, 0.0) in u['contour']
    assert (u['flat_length'] - 5, 0.0) in u['contour'] and (5.0, 1000.0) in u['contour']
    ch = _flashing_unfold({**BASE, 'corners': [{'kind': 'chamfer', 'w': 5, 'h': 8}, None, None, {'kind': 'none'}]})
    assert len(ch['contour']) == 5 and ch['contour'][:2] == [(0.0, 8.0), (5.0, 0.0)]
    # tapered: the cut meets the SLANTED right edge, so the point stays on the edge line
    t = _flashing_unfold({**BASE, 'flanges_end': [50, 60], 'corners': [None, rect, rect, None]})
    x = max(c[0] for c in t['contour'] if c[1] == 8.0)
    assert x == pytest.approx(t['flat_length'] + 30 * 8 / 1000, abs=1e-3)
    with pytest.raises(ValueError):  # two cuts on one end wider than the flat together
        _flashing_unfold({**BASE, 'corners': [{'kind': 'rect', 'w': 40, 'h': 8}, {'kind': 'rect', 'w': 40, 'h': 8}]})
    with pytest.raises(ValueError):  # runs into the first bend relief
        _flashing_unfold({**BASE, 'relief': True, 'relief_width': 3, 'relief_depth': 4,
                          'corners': [{'kind': 'rect', 'w': 47, 'h': 8}]})


def test_corner_cut_width_up_to_a_bend_line():
    line = _flashing_unfold(BASE)['bends'][0]['line']
    u = _flashing_unfold({**BASE, 'corners': [{'kind': 'rect', 'to_bend': 1, 'h': 8}, None, None, None]})
    assert (line, 0.0) in u['contour']  # left cut stops exactly on the bend line
    assert u['corner_cuts'][0]['w'] == pytest.approx(line, abs=1e-3)  # resolved width is reported back for the UI
    u = _flashing_unfold({**BASE, 'corners': [None, {'kind': 'rect', 'to_bend': 1, 'h': 8}, None, None]})
    assert (line, 0.0) in u['contour']  # right cut reaches back to the same line
    # with relief notches it stops at the notch edge (line - relief/2) so the outline never folds back
    r = _flashing_unfold({**BASE, 'relief': True, 'relief_width': 3, 'relief_depth': 4,
                          'corners': [{'kind': 'rect', 'to_bend': 1, 'h': 8}, None, None, None]})
    assert (line - 1.5, 4.0) in r['contour']  # the cut meets the notch wall (the retraced edge between them is dropped)
    with pytest.raises(ValueError):
        _flashing_unfold({**BASE, 'corners': [{'kind': 'rect', 'to_bend': 5, 'h': 8}]})


def test_knots_map_flat_pattern_onto_profile():
    u = _flashing_unfold({**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90]})
    k = u['knots']
    assert len(k) == 2 * len(u['flanges']) and k[0][0] == 0 and k[-1][0] == u['flat_length']
    # web (side 2): 60 outside minus 2*(r+t)=8 -> 52 straight, on the profile and in the flat alike
    web = ((k[3][1] - k[2][1]) ** 2 + (k[3][2] - k[2][2]) ** 2) ** 0.5
    assert web == pytest.approx(52, abs=1e-3) and k[3][0] - k[2][0] == pytest.approx(52, abs=1e-3)
    t = _flashing_unfold({**BASE, 'flanges_end': [50, 60]})
    assert len(t['knots_end']) == len(t['knots'])
    h = _flashing_unfold({**BASE, 'hem_end': True, 'hem_length': 8})
    assert len(h['knots']) == 6  # the hem is its own knot pair, drawn beside the side it folds onto


def test_end_cuts_between_bend_lines():
    u0 = _flashing_unfold({**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90]})
    a, b = u0['bends'][0]['line'], u0['bends'][1]['line']
    u = _flashing_unfold({**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90],
                          'end_cuts': [{'end': 'start', 'from': 'b1', 'to': 'b2', 'd': 10}]})
    assert [(a, 0.0), (a, 10.0), (b, 10.0), (b, 0.0)] == [c for c in u['contour'] if c[0] in (a, b) and c[1] in (0.0, 10.0)]
    # far end, typed width measured from a line; flat edge as the other limit
    v = _flashing_unfold({**BASE, 'end_cuts': [{'end': 'end', 'from': 'b1', 'to': 'w', 'w': 10, 'd': 7}]})
    line = v['bends'][0]['line']
    assert (line, 1000.0) in v['contour'] and (line + 10, 993.0) in v['contour']
    # a relief notch inside a deeper end cut is swallowed by it
    r = _flashing_unfold({**BASE, 'relief': True, 'relief_width': 3, 'relief_depth': 4,
                          'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'e1', 'd': 6}]})
    assert len(r['contour']) == 8 and all(a != b for a, b in zip(r['contour'], r['contour'][1:]))  # whole-width cut + far-end relief only
    with pytest.raises(ValueError):
        _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'e1', 'to': 'b1', 'd': 5}]})  # reversed limits
    with pytest.raises(ValueError):
        _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'w', 'w': 20, 'd': 5},
                                               {'end': 'start', 'from': 'e0', 'to': 'w', 'w': 10, 'd': 5}]})


def test_bend_lines_are_cut_where_material_is_removed():
    u = _flashing_unfold({**BASE, 'relief': True, 'relief_width': 3, 'relief_depth': 4})
    assert (u['bends'][0]['y0'], u['bends'][0]['y1']) == (4.0, 996.0)  # line passes through the relief notches
    c = _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'e1', 'd': 30}]})
    assert (c['bends'][0]['y0'], c['bends'][0]['y1']) == (30.0, 1000.0)
    k = _flashing_unfold({**BASE, 'corners': [{'kind': 'rect', 'w': 60, 'h': 12}, None, None, None]})
    assert k['bends'][0]['y0'] == 12.0  # corner cut wider than the line's position swallows its start
    a = _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'b1', 'd': 30}]})
    assert a['bends'][0]['y0'] == 0.0  # a cut that only ENDS on the line leaves the line intact


def test_edge_to_edge_cut_leaves_no_sliver_edges(client):
    u = _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'e1', 'd': 30}]})
    f = u['flat_length']
    assert u['contour'] == [(0.0, 30.0), (f, 30.0), (f, 1000.0), (0.0, 1000.0)]
    both = _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'e1', 'd': 30},
                                                  {'end': 'end', 'from': 'e0', 'to': 'e1', 'd': 20}]})
    assert both['contour'] == [(0.0, 30.0), (f, 30.0), (f, 980.0), (0.0, 980.0)]
    res = client.post('/api/flashing/dxf', json={**BASE, 'end_cuts': [{'end': 'start', 'from': 'e0', 'to': 'e1', 'd': 30}]})
    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    ys = sorted({round(v[1]) for e in doc.modelspace() if e.dxftype() == 'LWPOLYLINE' for v in e.get_points()})
    assert ys == [30, 1000]  # nothing left below the cut


def test_end_cut_offsets():
    u0 = _flashing_unfold({**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90]})
    a, b = u0['bends'][0]['line'], u0['bends'][1]['line']
    u = _flashing_unfold({**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90],
                          'end_cuts': [{'end': 'start', 'from': 'b1', 'to': 'b2', 'd': 10, 'off0': 3, 'off1': 4}]})
    assert (a + 3, 10.0) in u['contour'] and (b - 4, 10.0) in u['contour']
    w = _flashing_unfold({**BASE, 'end_cuts': [{'end': 'start', 'from': 'b1', 'to': 'w', 'w': 10, 'd': 5, 'off0': 2}]})
    assert (w['bends'][0]['line'] + 2, 5.0) in w['contour'] and (w['bends'][0]['line'] + 12, 5.0) in w['contour']
    with pytest.raises(ValueError):  # offsets eat the whole span
        _flashing_unfold({**BASE, 'profile': 'U', 'flanges': [30, 60, 30], 'angles': [90, 90],
                          'end_cuts': [{'end': 'start', 'from': 'b1', 'to': 'b2', 'd': 10, 'off0': 30, 'off1': 30}]})


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
    assert doc.header.get('$INSUNITS') == 4 and doc.header.get('$MEASUREMENT') == 1  # millimetres, metric
    assert doc.header.get('$DIMLFAC', 1.0) == 1.0  # a 100x linear factor made SolidWorks import 100 mm as 10000 mm
    assert doc.layers.get('BEND_UP').dxf.linetype == 'DASHED' and 'DASHED' in doc.linetypes
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
