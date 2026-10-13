"""
pytest tests for the DXF geometry money path (cut length + pierce count ->
price):

1. SPLINE/ELLIPSE contours used to contribute zero length with no warning -
   process_entity() now flattens them into 'line' shapes (the viewer knows no
   other curve type) that feed length, pierce counting and the bounding box.
2. Anything process_entity() can't convert (HATCH, INSERT, 3DFACE...) or that
   raised mid-processing is reported through analyze_dxf_geometry()'s optional
   `report` dict and shown as a warning on the upload paths, instead of being
   silently priced as zero.
3. count_pierces() went from an O(n^2) pairwise comparison to a spatial hash +
   union-find - it must give exactly the counts the old one did (kept below as
   _reference_count_pierces), and analyze_dxf_geometry() refuses a drawing
   above DXF_MAX_SEGMENTS with a clear message rather than pinning the worker.

Run with:
    python -m pytest testing/test_dxf_curves_and_pierces.py -q -p no:cacheprovider
"""
import io
import math
import os
import random
import time

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

import ezdxf
import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from app import (app as flask_app, db, User, MaterialPrice, Service, limiter,
                 analyze_dxf_geometry, count_pierces, dxf_skipped_warning)


def _reference_count_pierces(all_segments, tolerance=0.5):
    """The previous O(n^2) implementation, verbatim - the oracle for the new one."""
    num_segs = len(all_segments)
    if num_segs == 0:
        return 0

    adj = {i: [] for i in range(num_segs)}
    for i in range(num_segs):
        s1, e1 = all_segments[i]
        for j in range(i + 1, num_segs):
            s2, e2 = all_segments[j]
            if (math.dist(s1, s2) <= tolerance or
                    math.dist(s1, e2) <= tolerance or
                    math.dist(e1, s2) <= tolerance or
                    math.dist(e1, e2) <= tolerance):
                adj[i].append(j)
                adj[j].append(i)

    pierce_count = 0
    visited = set()
    for node in range(num_segs):
        if node not in visited:
            pierce_count += 1
            queue = [node]
            visited.add(node)
            while queue:
                curr = queue.pop(0)
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

    return pierce_count


def _new_doc():
    doc = ezdxf.new('R2000')
    return doc, doc.modelspace()


def _analyze(doc, tmp_path):
    path = str(tmp_path / 'part.dxf')
    doc.saveas(path)
    report = {}
    return analyze_dxf_geometry(path, report), report


def _polyline_length(points):
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def _ellipse_perimeter(a, b, start=0.0, end=2 * math.pi, n=200_000):
    """Midpoint-rule arc length of x=a*cos(t), y=b*sin(t) - independent of ezdxf."""
    step = (end - start) / n
    return sum(math.hypot(a * math.sin(start + (i + 0.5) * step), b * math.cos(start + (i + 0.5) * step)) for i in range(n)) * step


# ----- curves -----

def test_closed_spline_is_priced_and_is_one_pierce(tmp_path):
    doc, msp = _new_doc()
    spline = msp.add_spline_control_frame([(0, 0), (60, 10), (80, 50), (30, 70), (-10, 40), (0, 0)])
    # Independent of the flattening under test: evaluate the B-spline itself, densely.
    dense = [(p.x, p.y) for p in spline.construction_tool().approximate(50_000)]
    expected = _polyline_length(dense)
    assert math.dist(dense[0], dense[-1]) < 1e-6   # really a closed contour

    (width, height, length, pierces, shapes), report = _analyze(doc, tmp_path)
    assert length == pytest.approx(expected, rel=0.01)
    assert length == pytest.approx(expected, rel=0.001)   # in fact far closer than the 1% requirement
    assert pierces == 1
    assert shapes and all(s['type'] == 'line' for s in shapes)
    assert width == pytest.approx(max(x for x, _ in dense) - min(x for x, _ in dense), abs=0.05)
    assert height == pytest.approx(max(y for _, y in dense) - min(y for _, y in dense), abs=0.05)
    assert report == {}


def test_full_ellipse_is_priced_and_is_one_pierce(tmp_path):
    doc, msp = _new_doc()
    msp.add_ellipse((10, 5), major_axis=(40, 0), ratio=0.5)
    (width, height, length, pierces, shapes), report = _analyze(doc, tmp_path)
    assert length == pytest.approx(_ellipse_perimeter(40, 20), rel=0.001)
    assert pierces == 1
    assert shapes and all(s['type'] == 'line' for s in shapes)
    assert (width, height) == (pytest.approx(80, abs=0.05), pytest.approx(40, abs=0.05))
    assert report == {}


def test_partial_ellipse_joins_the_lines_it_touches(tmp_path):
    doc, msp = _new_doc()
    msp.add_ellipse((0, 0), major_axis=(40, 0), ratio=0.5, start_param=0, end_param=math.pi / 2)   # (40,0) -> (0,20)
    (width, height, length, pierces, shapes), _ = _analyze(doc, tmp_path)
    quarter = _ellipse_perimeter(40, 20, 0, math.pi / 2)
    assert length == pytest.approx(quarter, rel=0.001)
    assert pierces == 1
    assert shapes and all(s['type'] == 'line' for s in shapes)
    assert (width, height) == (pytest.approx(40, abs=0.05), pytest.approx(20, abs=0.05))

    # Closed into a "pie slice" by two lines: still one contour, not three.
    msp.add_line((0, 0), (40, 0))
    msp.add_line((0, 0), (0, 20))
    (_, _, length, pierces, _), _ = _analyze(doc, tmp_path)
    assert length == pytest.approx(quarter + 60, rel=0.001)
    assert pierces == 1


def test_curve_on_a_bend_layer_is_still_not_a_cut(tmp_path):
    doc, msp = _new_doc()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True)
    msp.add_ellipse((50, 50), major_axis=(30, 0), ratio=0.5, dxfattribs={'layer': 'BEND_UP'})
    msp.add_3dface([(0, 0), (1, 0), (1, 1), (0, 1)], dxfattribs={'layer': 'bend_down'})
    (_, _, length, pierces, shapes), report = _analyze(doc, tmp_path)
    assert (length, pierces, len(shapes)) == (400.0, 1, 4)
    assert report == {}


# ----- skipped / failed entities -----

def test_unsupported_entities_are_reported_and_the_rest_is_priced(tmp_path):
    doc, msp = _new_doc()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True)
    for _ in range(3):
        msp.add_hatch().paths.add_polyline_path([(10, 10), (20, 10), (20, 20)])
    doc.blocks.new('HOLE').add_circle((0, 0), 5)
    msp.add_blockref('HOLE', (50, 50))
    msp.add_polyline3d([(0, 0, 0), (10, 10, 10)])
    msp.add_text('not a cut')          # DXF_NON_CUT_TYPES: no warning
    msp.add_point((1, 1))

    (width, height, length, pierces, shapes), report = _analyze(doc, tmp_path)
    assert (width, height, length, pierces) == (100.0, 100.0, 400.0, 1)
    assert len(shapes) == 4
    assert report == {'skipped': {'HATCH': 3, 'INSERT': 1, 'POLYLINE (3D)': 1}}
    warning = dxf_skipped_warning(report)
    assert 'HATCH ×3, INSERT ×1' in warning and 'Проверете дължината на рязане.' in warning


def test_entity_that_raises_is_reported_not_swallowed(tmp_path):
    doc, msp = _new_doc()
    msp.add_line((0, 0), (10, 0))
    msp.add_spline()   # no control or fit points - ezdxf can't build a curve from it
    (_, _, length, pierces, _), report = _analyze(doc, tmp_path)
    assert (length, pierces) == (10.0, 1)
    assert report == {'failed': {'SPLINE': 1}}
    assert 'SPLINE ×1' in dxf_skipped_warning(report)


def test_clean_drawing_has_no_warning_and_old_call_signature_still_works(tmp_path):
    doc, msp = _new_doc()
    msp.add_circle((0, 0), 10)
    path = str(tmp_path / 'c.dxf')
    doc.saveas(path)
    assert analyze_dxf_geometry(path) == (20.0, 20.0, round(2 * math.pi * 10, 2), 1, [{'type': 'circle', 'cx': 0.0, 'cy': 0.0, 'r': 10.0}])
    assert dxf_skipped_warning({}) is None


# ----- count_pierces -----

def test_count_pierces_matches_the_old_implementation():
    rng = random.Random(20261013)
    # Coordinates on a 0.25 mm lattice put many endpoints EXACTLY at the
    # tolerance from each other; the nudges put others a hair either side of it.
    nudges = (0.0, 0.0, 0.0, 1e-9, -1e-9, 1e-13, 0.5, 0.5 + 1e-12, 0.5 - 1e-12, 0.353553, 0.36)

    def coord(span):
        return rng.randrange(-span, span) * 0.25 + rng.choice(nudges)

    def point(span, pool):
        if pool and rng.random() < 0.3:
            return rng.choice(pool)   # shared endpoint, like consecutive polyline edges
        p = (coord(span), coord(span))
        pool.append(p)
        return p

    for case in range(400):
        span = rng.choice((2, 4, 8, 40, 400))   # from everything-in-one-cluster to sparse
        pool = []
        segments = []
        for _ in range(rng.randrange(0, 70)):
            start = point(span, pool)
            end = start if rng.random() < 0.1 else point(span, pool)   # start == end: a circle
            segments.append((start, end))
        for tolerance in (0.5, 0.3, 1.0, 0):
            expected = _reference_count_pierces(segments, tolerance)
            assert count_pierces(segments, tolerance) == expected, (case, tolerance, segments)
        assert count_pierces(segments) == _reference_count_pierces(segments)


def test_count_pierces_hand_checked_cases():
    assert count_pierces([]) == 0
    assert count_pierces([((0, 0), (10, 0)), ((10.5, 0), (20, 0))]) == 1         # exactly at the tolerance
    assert count_pierces([((0, 0), (10, 0)), ((10.5000001, 0), (20, 0))]) == 2   # just beyond it
    assert count_pierces([((0, 0), (0, 0)), ((100, 100), (100, 100))]) == 2      # two circles
    assert count_pierces([((-0.15, -0.15), (5, 5)), ((0.15, 0.15), (-5, 5))]) == 1   # 0.42 mm apart, across the origin's cell borders
    assert count_pierces([((-0.2, -0.2), (5, 5)), ((0.2, 0.2), (-5, 5))]) == 2       # 0.57 mm apart


def test_count_pierces_50k_segments_is_fast():
    # 12 500 separate 4-segment squares, 10 mm apart.
    segments = []
    for i in range(12_500):
        x, y = (i % 125) * 10.0, (i // 125) * 10.0
        corners = [(x, y), (x + 5, y), (x + 5, y + 5), (x, y + 5)]
        segments += [(corners[k], corners[(k + 1) % 4]) for k in range(4)]
    assert len(segments) == 50_000
    started = time.perf_counter()
    assert count_pierces(segments) == 12_500
    assert time.perf_counter() - started < 2.0

    # The worst realistic shape for a grid: a drawing in metres, so thousands
    # of distinct endpoints land in a handful of cells.
    rng = random.Random(1)
    dense = [((rng.random() * 2, rng.random()), (rng.random() * 2, rng.random())) for _ in range(25_000)]
    started = time.perf_counter()
    assert count_pierces(dense) == 1
    assert time.perf_counter() - started < 2.0


# ----- the cap + the user-facing paths -----

def test_drawing_above_the_segment_cap_is_refused_with_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'DXF_MAX_SEGMENTS', 10)
    doc, msp = _new_doc()
    for i in range(11):
        msp.add_line((i, 0), (i, 5))
    (width, height, length, pierces, shapes), report = _analyze(doc, tmp_path)
    assert (width, height, length, pierces, shapes) == (None, None, None, None, None)
    assert 'твърде сложен' in report['error'] and 'над 10 сегмента' in report['error']

    # One flattened curve alone can exceed it too (its pieces are shapes, not pierce segments).
    doc, msp = _new_doc()
    msp.add_ellipse((0, 0), major_axis=(40, 0), ratio=0.5)
    (width, *_), report = _analyze(doc, tmp_path)
    assert width is None and 'error' in report


def _dxf_bytes(doc):
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add_all([
            User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'),
            MaterialPrice(key='qa_mat', display_name='QA Material', cost_per_m2=10, cutting_speed_mm_per_min=1000, pierce_rate_per_min=10),
            Service(name='QA Service', price_per_hour_eur=60),
        ])
        db.session.commit()
        service_id = Service.query.first().id
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c, service_id
        db.session.remove()
        db.drop_all()


def _drawing_with_hatch():
    doc, msp = _new_doc()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True)
    msp.add_hatch().paths.add_polyline_path([(10, 10), (20, 10), (20, 20)])
    return doc


def test_quick_create_detail_returns_the_warning_and_the_cap_message(client, monkeypatch):
    c, _service_id = client
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Hatch', 'material': 'qa_mat', 'file': (io.BytesIO(_dxf_bytes(_drawing_with_hatch())), 'part.dxf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    assert 'HATCH ×1' in res.get_json()['warning']

    clean, msp = _new_doc()
    msp.add_line((0, 0), (10, 0))
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Clean', 'material': 'qa_mat', 'file': (io.BytesIO(_dxf_bytes(clean)), 'part.dxf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 200 and res.get_json()['warning'] is None

    monkeypatch.setattr(app_module, 'DXF_MAX_SEGMENTS', 2)
    res = c.post('/api/quick-create-detail', data={
        'name': 'QA Huge', 'material': 'qa_mat', 'file': (io.BytesIO(_dxf_bytes(_drawing_with_hatch())), 'part.dxf'),
    }, content_type='multipart/form-data')
    assert res.status_code == 400
    assert 'твърде сложен' in res.get_json()['message']


def test_upload_and_admin_add_detail_flash_the_warning(client, monkeypatch):
    c, service_id = client
    res = c.post('/upload', data={
        'material': 'qa_mat', 'service_ids': str(service_id), 'file': (io.BytesIO(_dxf_bytes(_drawing_with_hatch())), 'part.dxf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    page = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'HATCH ×1' in page and 'Проверете дължината на рязане.' in page

    res = c.post('/admin/details/add', data={
        'name': 'QA Flash', 'material': 'qa_mat', 'file': (io.BytesIO(_dxf_bytes(_drawing_with_hatch())), 'part.dxf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert res.status_code == 200 and 'HATCH ×1' in res.get_data(as_text=True)

    monkeypatch.setattr(app_module, 'DXF_MAX_SEGMENTS', 2)
    res = c.post('/upload', data={
        'material': 'qa_mat', 'service_ids': str(service_id), 'file': (io.BytesIO(_dxf_bytes(_drawing_with_hatch())), 'part.dxf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert res.status_code == 200 and 'твърде сложен' in res.get_data(as_text=True)
