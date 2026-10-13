"""pytest: STEP import of SolidWorks parts/assemblies - sheet-metal split, flat pattern (hand-computed), DXF round trip and the review/create routes.
Needs cadquery-ocp. Run: pytest testing/test_step_import.py -v"""
import io
import math
import os
import tempfile

_fd, _path = tempfile.mkstemp(suffix='.db')
os.close(_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_path}'

import pytest

pytest.importorskip('OCP')
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from app import app, db, Detail, DetailDxfFile, MaterialPrice, Product, ProductDetail, User, limiter
from bending import step_import
from bending.dxf_bends import bends_from_lines
from testing.step_fixtures import l_bracket, u_channel, write_step

BA = math.pi / 2 * (2 + 0.44 * 2)   # K-factor 0.44, 90 degrees, r = t = 2


@pytest.fixture(scope='module')
def asm(tmp_path_factory):
    d = tmp_path_factory.mktemp('step')
    p = str(d / 'asm.step')
    write_step(p, [('Конзола', l_bracket(), [(0, 0, 0), (100, 0, 0)]), ('U профил', u_channel(), [(0, 200, 0)]), ('Блок', BRepPrimAPI_MakeBox(30, 30, 30).Shape(), [(0, 100, 0)])])
    return p


def test_assembly_is_split_into_parts_with_quantities(asm):
    parts = {p['name']: p for p in step_import.read_parts(asm)}
    assert set(parts) == {'Конзола', 'U профил', 'Блок'} and parts['Конзола']['qty'] == 2 and parts['Блок']['qty'] == 1
    assert step_import.analyze(parts['Конзола']['shape'])['sheet'] and step_import.analyze(parts['U профил']['shape'])['bends'] == 2
    block = step_import.analyze(parts['Блок']['shape'])
    assert not block['sheet'] and block['why']


def test_l_bracket_flat_pattern(asm):
    flat = step_import.flat_pattern({p['name']: p for p in step_import.read_parts(asm)}['Конзола']['shape'])
    w, h = flat['size']
    assert w == pytest.approx(50.0) and h == pytest.approx((40 - 4) + BA + (30 - 4), abs=1e-3)   # two straight parts + the bend allowance
    (b,) = flat['bends']
    assert b['dir'] == 'down' and b['angle'] == pytest.approx(90.0, abs=1e-3) and b['p0'][1] == pytest.approx(36 + BA / 2, abs=1e-3)
    holes = [e for e in flat['cut'] if e[0] == 'circle']
    assert len(holes) == 1 and holes[0][2] == pytest.approx(3.0) and holes[0][1] == pytest.approx((25.0, 12.0), abs=1e-3)   # 12 from the flange end, centred


def test_u_channel_two_bends(asm):
    flat = step_import.flat_pattern({p['name']: p for p in step_import.read_parts(asm)}['U профил']['shape'])
    assert flat['size'][1] == pytest.approx(2 * 26 + 52 + 2 * BA, abs=1e-3) and len(flat['bends']) == 2
    (hole,) = [e for e in flat['cut'] if e[0] == 'circle']
    assert hole[2] == pytest.approx(2.5) and min(abs(hole[1][1] - flat['size'][1]), abs(hole[1][1])) == pytest.approx(20.0, abs=1e-3)   # 20 from a leg end


def test_dxf_round_trip_gives_bend_steps(asm, tmp_path):
    import ezdxf
    from app import analyze_dxf_geometry
    flat = step_import.flat_pattern({p['name']: p for p in step_import.read_parts(asm)}['U профил']['shape'])
    out = str(tmp_path / 'u.dxf')
    step_import.write_dxf(flat, out)
    width, height, length, pierces, _ = analyze_dxf_geometry(out)
    assert max(width, height) == pytest.approx(flat['size'][1], abs=0.05) and pierces == 2   # outline + one hole; bend layers are not priced
    msp = ezdxf.readfile(out).modelspace()
    lines = [(e.dxf.layer, tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2]) for e in msp if e.dxftype() == 'LINE' and e.dxf.layer.startswith('BEND')]
    outline = [tuple(e.dxf.start)[:2] for e in msp if e.dxftype() == 'LINE' and e.dxf.layer == 'CUT']
    bends, flat_len = bends_from_lines(lines, (0, 0), flat['size'])
    assert len(bends) == 2 and all(b['angle'] == 90 and b['dir'] == 'down' and b['radius'] == 2.0 for b in bends) and outline   # the radius of the model is in the layer name


def test_native_solidworks_files_are_explained(asm):
    with app.app_context():
        db.create_all()
        db.session.add(User(username='st_a', password='x', role='admin'))
        db.session.commit()
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        limiter.reset()
        from werkzeug.security import generate_password_hash
        User.query.filter_by(username='st_a').one().password = generate_password_hash('irrelevant123')
        db.session.commit()
        c = app.test_client()
        c.post('/login', data={'username': 'st_a', 'password': 'irrelevant123'})
        r = c.post('/admin/bending/step-import', data={'file': (io.BytesIO(b'x'), 'part.sldprt')}, content_type='multipart/form-data')
        assert 'затворен формат'.encode() in r.data
        with open(asm, 'rb') as f:
            r = c.post('/admin/bending/step-import', data={'file': (f, 'Сглобка.step')}, content_type='multipart/form-data')
        html = r.get_data(as_text=True)
        assert 'Конзола' in html and 'Не е ламарина' in html and 'name="sel"' in html
        token = html.split('name="token" value="')[1].split('"')[0]
        db.session.add(MaterialPrice(key='st2', display_name='Ст 2мм', type='sheets', cost_per_m2=10.0, thickness_mm=2.0))
        db.session.commit()
        names = {p['name']: i for i, p in enumerate(step_import.read_parts(asm))}
        data = {'token': token, 'sel': [str(names['Конзола']), str(names['U профил'])], 'product_name': 'Сглобка',
                f"material_{names['Конзола']}": 'st2', f"material_{names['U профил']}": 'st2'}
        r = c.post('/admin/bending/step-import/create', data=data)
        assert r.status_code == 302
        det = {d.name: d for d in Detail.query.all()}
        assert set(det) == {'Конзола', 'U профил'} and det['Конзола'].thickness_mm == 2.0
        assert DetailDxfFile.query.count() == 4   # a DXF development and the part's own STEP for each detail
        step = DetailDxfFile.query.filter(DetailDxfFile.original_filename.like('%.step'), DetailDxfFile.detail_id == det['Конзола'].id).one()
        assert os.path.getsize(os.path.join(app.config['DETAIL_DXF_FOLDER'], step.filename)) > 1000
        m = c.get(f'/details/files/{step.id}/mesh').get_json()
        assert len(m['indices']) % 3 == 0 and len(m['indices']) > 100 and max(m['indices']) < len(m['positions']) // 3 and m['bbox'][1][0] == pytest.approx(40.0, abs=0.01)
        assert b'three' in c.get(f'/details/files/{step.id}/view3d').data
        dxf = DetailDxfFile.query.filter(DetailDxfFile.original_filename.like('%.dxf')).first()
        assert c.get(f'/details/files/{dxf.id}/mesh').status_code == 404   # only STEP files
        for f in DetailDxfFile.query.all():
            os.remove(os.path.join(app.config['DETAIL_DXF_FOLDER'], f.filename))
        prod = Product.query.one()
        assert {pd.detail.name: pd.quantity for pd in ProductDetail.query.filter_by(product_id=prod.id)} == {'Конзола': 2, 'U профил': 1}
        db.session.remove()
        db.drop_all()
