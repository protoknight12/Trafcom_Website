"""
pytest regression test for api_generator_dxf() (/api/generator/dxf) - the
Panel Generator's DXF export moved server-side to ezdxf's writer after the
old hand-rolled DXF text (built in templates/generator.html) was found to
skip sections/tables (BLOCK_RECORD, OBJECTS, entity handles) that real CAD
software expects, even though our own reader tolerated it fine. Guards that
the endpoint requires login, rejects bad dimensions, and produces a DXF that
round-trips through ezdxf with the expected entity count.

Run with:
    pytest testing/test_generator_dxf_export.py -v
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
        pass  # Windows keeps the file locked as long as SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import ezdxf
import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        user = User(username='qa_gen', password=generate_password_hash('irrelevant123'), role='regular_user')
        db.session.add(user)
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'qa_gen', 'password': 'irrelevant123'})
    return c


def test_requires_login(app):
    res = app.test_client().post('/api/generator/dxf', json={'width': 100, 'height': 50, 'holes': []})
    assert res.status_code in (302, 401)


def test_rejects_bad_dimensions(client):
    res = client.post('/api/generator/dxf', json={'width': 'nope', 'height': 50, 'holes': []})
    assert res.status_code == 400


def test_produces_valid_dxf_with_holes(client):
    holes = [
        {'x': 30, 'y': 20, 'size': 10, 'rot': 0, 'type': 'circle'},
        {'x': 60, 'y': 20, 'size': 10, 'rot': 0.4, 'type': 'hexagon'},
        {'x': 90, 'y': 20, 'size': 10, 'rot': 0, 'type': 'hexcluster'},
    ]
    res = client.post('/api/generator/dxf', json={'width': 150, 'height': 40, 'holes': holes})
    assert res.status_code == 200
    assert res.content_type == 'application/dxf'
    assert 'Panel_150x40_Mixed.dxf' in res.headers.get('Content-Disposition', '')

    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    msp = doc.modelspace()
    # 1 border + 1 circle + 1 hexagon + 3 hexcluster rhombi = 6 entities
    assert len(msp) == 6
    assert doc.audit().errors == []


def test_dxf_units_are_millimeters(client):
    """Regression test: the exported DXF used to leave $INSUNITS at its
    ezdxf default (0 - unitless), which CAD software commonly interprets as
    inches, opening a panel drawn entirely in mm at 25.4x its real size."""
    res = client.post('/api/generator/dxf', json={'width': 100, 'height': 50, 'holes': []})
    assert res.status_code == 200

    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    assert doc.header.get('$INSUNITS') == 4  # ezdxf.units.MM
    assert doc.header.get('$MEASUREMENT') == 1  # metric


def test_hexcluster_cluster_mask_drops_rhombi(client):
    """'Пълнота на Hex Cluster' lets each of the 3 tumbling-block rhombi be
    independently dropped - guards that a partial mask actually cuts fewer
    rhombi, a full/omitted mask still cuts all 3 (unchanged default
    behavior), and a malformed mask from a tampered request is ignored
    rather than crashing the export. Each surviving rhombus stays its own
    separate piece (still shrunk toward its own centroid with the usual
    gap), even when two kept slots happen to be adjacent."""
    holes = [
        {'x': 30, 'y': 20, 'size': 10, 'rot': 0, 'type': 'hexcluster', 'clusterMask': [True, False, True]},
        {'x': 60, 'y': 20, 'size': 10, 'rot': 0, 'type': 'hexcluster'},
        {'x': 90, 'y': 20, 'size': 10, 'rot': 0, 'type': 'hexcluster', 'clusterMask': 'not-a-list'},
    ]
    res = client.post('/api/generator/dxf', json={'width': 120, 'height': 40, 'holes': holes})
    assert res.status_code == 200

    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    msp = doc.modelspace()
    # 1 border + 2 rhombi (masked) + 3 rhombi (no mask) + 3 rhombi (bad mask ignored) = 9
    assert len(msp) == 9
    assert doc.audit().errors == []

    masked_entities = list(msp)[1:3]
    for entity in masked_entities:
        assert len(entity.get_points('xy')) == 4


def test_hexcluster_partial_mask_keeps_rhombi_separate():
    """Each kept rhombus in a partial hex-cluster ("Пълнота на Hex Cluster"
    below 100%) is shrunk toward its own centroid independently, same as the
    classic 3-piece cube - two surviving rhombi that happen to sit next to
    each other (e.g. slots 0-1, 1-2, or 2-0) stay two separate 4-vertex
    pieces with the same "Разстояние между ромбовете" kerf as any other
    pair, never merged into one seamless piece."""
    from app import _generator_hole_polygon

    for mask in ([True, True, False], [False, True, True], [True, False, True],
                 [True, False, False], [False, True, False], [False, False, True]):
        n_kept = sum(mask)
        loops = _generator_hole_polygon('hexcluster', 30.0, cluster_mask=mask)
        assert len(loops) == n_kept
        assert all(len(loop) == 4 for loop in loops)

    assert len(_generator_hole_polygon('hexcluster', 30.0, cluster_mask=[True, True, True])) == 3
    assert len(_generator_hole_polygon('hexcluster', 30.0)) == 3


def test_produces_valid_dxf_with_slot_holes(client):
    """The 'slot' hole type (vertical rounded slots, the 'Дъжд' pattern
    added alongside the honeycomb generator) carries a `length` in addition
    to `size` (its width) - guards that it's read and turned into a closed,
    valid polygon rather than silently falling back to a plain circle."""
    holes = [
        {'x': 20, 'y': 50, 'size': 6, 'length': 80, 'rot': 0, 'type': 'slot'},
        {'x': 40, 'y': 50, 'size': 6, 'rot': 0, 'type': 'slot'},  # missing length -> falls back to a circle-sized slot
    ]
    res = client.post('/api/generator/dxf', json={'width': 60, 'height': 100, 'holes': holes})
    assert res.status_code == 200

    doc = ezdxf.read(io.StringIO(res.get_data(as_text=True)))
    msp = doc.modelspace()
    # 1 border + 2 slot polygons = 3 entities
    assert len(msp) == 3
    assert doc.audit().errors == []

    entities = list(msp)
    slot_with_length = entities[1].get_points('xy')
    xs = [p[0] for p in slot_with_length]
    ys = [p[1] for p in slot_with_length]
    # width 6 -> radius 3 either side of x=20; length 80 -> caps reach y=10..90
    assert min(xs) == pytest.approx(17, abs=0.01) and max(xs) == pytest.approx(23, abs=0.01)
    assert min(ys) == pytest.approx(10, abs=0.01) and max(ys) == pytest.approx(90, abs=0.01)
