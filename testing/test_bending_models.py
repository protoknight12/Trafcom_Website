"""pytest: bending models create, link and enforce their constraints. Run: pytest testing/test_bending_models.py -v"""
import os
import tempfile

_fd, _path = tempfile.mkstemp(suffix='.db')
os.close(_fd)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_path}'

import pytest
from sqlalchemy.exc import IntegrityError

from app import (app, db, Machine, PressBrakeConfig, BendToolCatalog, BendTool, BendToolSegment,
                 ToolStorageLocation, BendDeduction)
from bending.unfold import bd_table


@pytest.fixture
def ctx():
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def test_tool_chain_and_machine_config(ctx):
    m = Machine(name='Durma AD-R 40175')
    db.session.add(m)
    db.session.flush()
    db.session.add(PressBrakeConfig(machine_id=m.id, tonnage_t=175, working_length_mm=4050, controller='delem_da66t'))
    loc = ToolStorageLocation(zone='A', cabinet='1', shelf='2')
    tool = BendTool(catalog=BendToolCatalog(manufacturer='EURAM'), code='P-88', kind='die', v_opening_mm=16)
    db.session.add(BendToolSegment(tool=tool, length_mm=500, erp_number=1, location=loc))
    db.session.commit()
    assert m.press_brake.tonnage_t == 175
    seg = BendToolSegment.query.one()
    assert seg.tool.catalog.manufacturer == 'EURAM' and seg.location.zone == 'A'
    assert seg.status == 'in_stock' and seg.bend_count == 0 and seg.tool.status == 'new'


def test_one_config_per_machine_and_unique_segment_erp(ctx):
    m = Machine(name='M')
    db.session.add(m)
    db.session.flush()
    db.session.add_all([PressBrakeConfig(machine_id=m.id), PressBrakeConfig(machine_id=m.id)])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    t = BendTool(code='D', kind='die')
    db.session.add_all([BendToolSegment(tool=t, length_mm=100, erp_number=5), BendToolSegment(tool=t, length_mm=200, erp_number=5)])
    with pytest.raises(IntegrityError):
        db.session.commit()


def test_deduction_rows_feed_bd_table(ctx):
    db.session.add(BendDeduction(material_type='sheets', thickness_mm=2, radius_mm=2, angle_deg=90, deduction_mm=3.6))
    db.session.commit()

    def lookup(a, r, t):
        row = BendDeduction.query.filter_by(angle_deg=a, radius_mm=r, thickness_mm=t).first()
        return row and row.deduction_mm
    assert bd_table(lookup)(90, 2, 2, 4.0) == pytest.approx(8 - 3.6)


def test_admin_bending_routes(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, MaterialType, seed_material_types
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    seed_material_types()
    db.session.add_all([User(username='bq_admin', password=generate_password_hash('irrelevant123'), role='admin'),
                        User(username='bq_worker', password=generate_password_hash('irrelevant123'), role='worker')])
    db.session.commit()
    w = app.test_client()
    w.post('/login', data={'username': 'bq_worker', 'password': 'irrelevant123'})
    assert w.get('/admin/bending').status_code == 302  # not for non-admins
    from flask import g
    g.pop('_login_user', None)  # the fixture's one app context would otherwise keep the worker cached
    c = app.test_client()
    lr = c.post('/login', data={'username': 'bq_admin', 'password': 'irrelevant123'})
    assert lr.status_code == 302, lr.get_data(as_text=True)[:400]
    r = c.get('/admin/bending')
    assert r.status_code == 200, r.headers.get('Location')
    c.post('/admin/bending/tools/add', data={'code': 'P-88', 'kind': 'die', 'v_opening_mm': '16'})
    c.post('/admin/bending/tools/add', data={'code': '', 'kind': 'die'})  # rejected
    assert [t.code for t in BendTool.query.all()] == ['P-88']
    c.post('/admin/bending/deductions/add', data={'material_type': 'sheets', 'thickness_mm': '2', 'radius_mm': '2', 'angle_deg': '90', 'deduction_mm': '3.6'})
    c.post('/admin/bending/deductions/add', data={'material_type': 'nope', 'thickness_mm': '2', 'radius_mm': '2', 'angle_deg': '90', 'deduction_mm': '3.6'})
    assert BendDeduction.query.count() == 1
    assert b'P-88' in c.get('/admin/bending').data
    prof = c.get(f'/admin/bending/tools/{BendTool.query.one().id}/profile.svg')
    assert prof.status_code == 200 and prof.mimetype == 'image/svg+xml' and b'<path' in prof.data and b'/profile.svg' in c.get('/admin/bending').data
    c.post('/admin/bending/locations/add', data={'zone': 'A', 'cabinet': '1'})
    loc = ToolStorageLocation.query.one()
    tid = BendTool.query.one().id
    c.post('/admin/bending/segments/add', data={'tool_id': tid, 'length_mm': '500', 'location_id': loc.id})
    c.post('/admin/bending/segments/add', data={'tool_id': tid, 'length_mm': '-1'})  # rejected
    seg = BendToolSegment.query.one()
    assert seg.erp_number == 100001 and seg.location_id == loc.id
    from app import _next_erp_number, _erp_number_conflict
    assert _next_erp_number() == 100002 and _erp_number_conflict(100001)  # shared ERP space
    assert b'100001' in c.get('/admin/bending').data
    c.post(f'/admin/bending/tools/{BendTool.query.one().id}/delete')
    assert BendTool.query.count() == 1  # has segments -> refused
    c.post(f'/admin/bending/segments/{seg.id}/delete')
    assert BendToolSegment.query.count() == 0
    c.post(f'/admin/bending/deductions/{BendDeduction.query.one().id}/delete')
    assert BendDeduction.query.count() == 0


def test_tool_csv_parse():
    from app import _parse_bend_tool_csv
    rows = _parse_bend_tool_csv('code,kind,height_mm,v_opening_mm\nP1,punch,120,\nD1,die,,16\n')
    assert rows[0]['height_mm'] == 120 and rows[0]['v_opening_mm'] is None and rows[1]['v_opening_mm'] == 16
    for bad in ('code,kind\nP1,hammer\n', 'code,kind,height_mm\nP1,punch,abc\n', 'x,y\n1,2\n'):
        with pytest.raises(ValueError):
            _parse_bend_tool_csv(bad)


def test_import_and_approve_route(ctx):
    import io as _io
    from werkzeug.security import generate_password_hash
    from app import User, limiter
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    db.session.add(User(username='bq_imp', password=generate_password_hash('irrelevant123'), role='admin'))
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_imp', 'password': 'irrelevant123'})
    csv_ok = b'code,kind,height_mm\nP1,punch,120\nD1,die,60\n'
    c.post('/admin/bending/tools/import', data={'manufacturer': 'EURAM', 'file': (_io.BytesIO(csv_ok), 't.csv')}, content_type='multipart/form-data')
    c.post('/admin/bending/tools/import', data={'manufacturer': 'EURAM', 'file': (_io.BytesIO(b'code,kind\nP9,bad\n'), 'b.csv')}, content_type='multipart/form-data')
    assert BendTool.query.count() == 2 and BendToolCatalog.query.one().manufacturer == 'EURAM'  # bad file imported nothing
    t = BendTool.query.filter_by(code='P1').one()
    assert t.status == 'new'
    c.post(f'/admin/bending/tools/{t.id}/approve')
    assert BendTool.query.filter_by(code='P1').one().status == 'in_library'


def test_program_flow_and_sheet(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice, seed_material_types
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    db.session.add(User(username='bq_prog', password=generate_password_hash('irrelevant123'), role='admin'))
    m = Machine(name='Durma')
    db.session.add(m)
    db.session.add(MaterialPrice(key='mat1', display_name='M', type='sheets', cost_per_m2=1))
    db.session.flush()
    d = Detail(name='Скоба', material_key='mat1', width=50, height=30, total_length=160, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_prog', 'password': 'irrelevant123'})
    r = c.post('/admin/bending/programs/add', data={'detail_id': d.id, 'machine_id': m.id})
    prog = BendProgram.query.one()
    assert prog.name == 'Скоба' and r.status_code == 302
    c.post(f'/admin/bending/programs/{prog.id}/status', data={'status': 'approved'})
    db.session.expire_all()
    assert prog.status == 'draft'  # no steps -> refused
    for a in ('90', '45', '200'):  # 200 is out of range
        c.post(f'/admin/bending/programs/{prog.id}/steps/add', data={'angle_deg': a, 'x_mm': '30'})
    db.session.expire_all()  # the test's copy of prog predates the requests' commits
    assert [s.angle_deg for s in prog.steps] == [90, 45] and [s.seq for s in prog.steps] == [1, 2]
    c.post(f'/admin/bending/steps/{prog.steps[0].id}/delete')
    db.session.expire_all()
    assert [(s.seq, s.angle_deg) for s in prog.steps] == [(1, 45)]
    c.post(f'/admin/bending/programs/{prog.id}/status', data={'status': 'approved'})
    db.session.expire_all()
    assert prog.status == 'approved'
    c.post(f'/admin/bending/programs/{prog.id}/steps/add', data={'angle_deg': '10'})
    db.session.expire_all()
    assert len(prog.steps) == 1  # approved program is frozen
    from app import ControlledDocument
    doc = ControlledDocument.query.one()
    assert doc.document_no == f'BP-{prog.id}' and doc.status == 'active' and doc.current_revision == '1' and prog.document_id == doc.id
    # new version: draft copy sharing the document; approving it retires v1 and adds a revision
    c.post(f'/admin/bending/programs/{prog.id}/new-version')
    db.session.expire_all()
    v2 = BendProgram.query.filter_by(version=2).one()
    assert v2.status == 'draft' and v2.document_id == doc.id and [(s.seq, s.angle_deg) for s in v2.steps] == [(1, 45)] and prog.status == 'approved'
    c.post(f'/admin/bending/programs/{v2.id}/status', data={'status': 'approved'})
    db.session.expire_all()
    assert prog.status == 'obsolete' and v2.status == 'approved' and doc.current_revision == '2' and len(doc.revisions) == 2
    c.post(f'/admin/bending/programs/{v2.id}/status', data={'status': 'draft'})  # an approved program can't go back to draft
    db.session.expire_all()
    assert v2.status == 'approved'
    prog, sid = v2, v2.steps[0].id
    ex = f'/bending/programs/{prog.id}/execute'
    assert c.get(ex).status_code == 200
    from app import MeasuringInstrument
    inst = MeasuringInstrument(name='Ъгломер')
    db.session.add(inst)
    db.session.commit()
    c.post(ex, data={'order_no': 'PO-7', f'm{sid}': '45.5', 'instrument_id': inst.id})  # nominal 45, tol 1 -> pass
    c.post(ex, data={f'm{sid}': '47'})  # out of tolerance -> fail
    c.post(ex, data={})  # nothing measured -> refused
    db.session.expire_all()
    from app import BendExecution, QualityCheck
    assert [(e.quality_check.overall_result, e.program_version) for e in BendExecution.query.order_by(BendExecution.id)] == [('pass', 2), ('fail', 2)]  # run on the approved v2
    q = QualityCheck.query.first()
    assert q.measurements[0].instrument.name == 'Ъгломер'
    assert 'годен'.encode() in c.get(f'/admin/bending/programs/{prog.id}').data and b'PO-7' in c.get(f'/admin/bending/programs/{prog.id}').data
    assert q.order_no == 'PO-7' and q.measurements[0].nominal_value == 45 and q.measurements[0].samples[0].value == 45.5
    sheet = c.get(f'/admin/bending/programs/{prog.id}/sheet')
    assert sheet.status_code == 200 and 'ОДОБРЕНА'.encode() in sheet.data and c.get('/admin/bending/programs').status_code == 200


def test_cannot_execute_draft_program(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, MaterialPrice
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    db.session.add_all([User(username='bq_ex', password=generate_password_hash('irrelevant123'), role='worker'), Machine(name='M'),
                        MaterialPrice(key='k', display_name='M', type='sheets', cost_per_m2=1)])
    db.session.flush()
    d = Detail(name='D', material_key='k', width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=Machine.query.one(), name='P')
    db.session.add(p)
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_ex', 'password': 'irrelevant123'})
    assert c.get(f'/bending/programs/{p.id}/execute').status_code == 302  # draft -> back to the sheet


def test_force_endpoint(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, MaterialPrice
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='M')
    db.session.add_all([User(username='bq_f', password=generate_password_hash('irrelevant123'), role='admin'), m,
                        MaterialPrice(key='k', display_name='M', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add(PressBrakeConfig(machine=m, tonnage_t=10))
    d = Detail(name='D', material_key='k', width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=m, name='P')
    db.session.add_all([p, BendTool(code='V12', kind='die', v_opening_mm=12, status='in_library'), BendTool(code='V16', kind='die', v_opening_mm=16, status='new')])
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_f', 'password': 'irrelevant123'})
    j = c.get(f'/admin/bending/programs/{p.id}/force?rm=400&t=2&length=1000').get_json()
    assert j['recommended_v'] == 12 and [d['code'] for d in j['dies']] == ['V12']  # unapproved V16 is not offered
    assert j['dies'][0]['force_kn_per_m'] == 189.3 and j['machine_kn'] == pytest.approx(98.0665)
    assert not j['dies'][0]['ok']  # 189 kN for 1 m > a 10 t machine
    assert c.get(f'/admin/bending/programs/{p.id}/force?rm=0&t=2&length=1000').status_code == 400


def test_seed_m460r(ctx):
    from migration.seed_bend_tools_m460r import seed
    from bending.profile import derived, radii_report
    assert seed() == (2, 54)                      # the punch and ONE four-way die M460.r, 27 segments each (3 sets x 9 lengths)
    assert seed() == (0, 0)  # idempotent
    BendToolSegment.query.filter_by(length_mm=415).delete()  # as if seeded before 415 was added
    db.session.commit()
    assert seed() == (0, 6) and BendToolSegment.query.count() == 54  # tops up only what's missing
    from app import BendTool as _T
    p = _T.query.filter_by(code='P.97.75.r08').one()
    assert (p.kind, p.height_mm, p.angle_deg, p.radius_mm, round(p.max_kn_per_m, 1), len(p.segments)) == ('punch', 67.6, 75, 0.8, 980.7, 27)
    pp = p.profile
    dv = derived(pp, 'punch')
    assert abs(dv['tip_angle'] - 75.0) < 0.05 and dv['tip_radius'] == 0.8 and abs(dv['min_y']) < 0.02 and 66.9 < dv['max_y'] < 67.2  # real tip, seat at 67.09
    assert [(r['kind'], r['radius']) for r in radii_report(pp)] == [('fillet', 0.8), ('arc', 24.6)]  # tip radius and the relief are data, not a drawing
    d = _T.query.filter_by(code='M460.r').one()
    assert d.kind == 'die' and d.max_kn_per_m == 800 and len(d.segments) == 27 and _T.query.filter(_T.code.like('M460.r V%')).count() == 0
    dd = derived(d.profile, 'die')
    assert (dd['width'], dd['height'], dd['base_width']) == (60, 60, 60)
    assert [(g['v'], g['shoulder_radii'][0]) for g in d.positions and dd['grooves']] == [(16, 2.0), (22, 2.0), (35, 2.0), (50, 3.0)]
    assert [p_['label'] for p_ in d.positions] == ['V16', 'V22', 'V35', 'V50']
    assert len({s.erp_number for s in BendToolSegment.query.all()}) == 54


def test_seed_merges_the_four_dies_an_earlier_seed_made(ctx):
    from migration.seed_bend_tools_m460r import seed
    from app import BendTool as _T
    for v in (16, 22, 35, 50):  # what the first version created: four separate dies with their own segments
        t = _T(code=f'M460.r V{v}', kind='die', v_opening_mm=v, radius_mm=2, angle_deg=85, status='in_library')
        db.session.add(t)
        db.session.add_all(BendToolSegment(tool=t, length_mm=10 * (i + 1), erp_number=500000 + v * 10 + i) for i in range(3))
    db.session.commit()
    seed()
    d = _T.query.filter_by(code='M460.r').one()
    assert _T.query.filter(_T.code.like('M460.r V%')).count() == 0 and d.v_opening_mm is None and len(d.positions) == 4
    assert BendToolSegment.query.filter_by(tool_id=d.id, length_mm=10).count() == 3   # the kept die keeps its segments (and their ERP numbers)
    assert any(s.erp_number == 500160 for s in d.segments) and BendToolSegment.query.filter(BendToolSegment.tool_id.is_(None)).count() == 0


def test_seed_refuses_to_merge_a_used_die(ctx):
    from migration.seed_bend_tools_m460r import seed
    from app import BendTool as _T
    for v in (16, 22):
        t = _T(code=f'M460.r V{v}', kind='die', v_opening_mm=v, status='in_library')
        db.session.add(t)
        db.session.add(BendToolSegment(tool=t, length_mm=10, erp_number=600000 + v, status='on_machine' if v == 22 else 'in_stock'))
    db.session.commit()
    with pytest.raises(SystemExit):
        seed()
    assert _T.query.filter(_T.code.like('M460.r V%')).count() == 2   # nothing was touched

def test_profile_editor_routes(ctx):
    import io as _io
    import json as _json
    import ezdxf as _ezdxf
    from werkzeug.security import generate_password_hash
    from app import User, limiter
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    db.session.add_all([User(username='bq_pe', password=generate_password_hash('irrelevant123'), role='admin'),
                        BendTool(code='P1', kind='punch', angle_deg=75, radius_mm=0.8, height_mm=67.6)])
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_pe', 'password': 'irrelevant123'})
    tid = BendTool.query.filter_by(code='P1').one().id
    assert c.get(f'/admin/bending/tools/{tid}/profile/edit').status_code == 200  # starts from the numbers
    assert BendTool.query.filter_by(code='P1').one().profile is None
    svg0 = c.get(f'/admin/bending/tools/{tid}/profile.svg').data
    assert svg0.count(b'<path') == 3  # a tool without a profile still gets a picture from its numbers (raster + clamp + outline)
    bad = c.post(f'/admin/bending/tools/{tid}/profile', json={'points': [[0, 0], [10, 0], [20, 0]]})
    assert bad.status_code == 400 and BendTool.query.filter_by(code='P1').one().profile is None
    ok = c.post(f'/admin/bending/tools/{tid}/profile', json={'points': [[0, 0], [8, 10], [8, 60], [-8, 60], [-8, 10]]})
    assert ok.status_code == 200 and len(BendTool.query.filter_by(code='P1').one().profile) == 5 and BendTool.query.filter_by(code='P1').one().profile[0] == [0, 0, 0, 0]
    assert c.get(f'/admin/bending/tools/{tid}/profile.svg').data.count(b'<path') == 3  # real outline + 5 mm raster + the standard clamp
    assert b'8' in c.get(f'/admin/bending/tools/{tid}/profile/edit').data
    doc = _ezdxf.new()
    # a full catalogue outline: 5 mm working part + a tang above the seat; the tool's nominal height 67.6 -> seat 67.09 above its lowest point
    doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 67.09), (6, 67.09), (6, 97), (0, 97)], close=True)
    buf = _io.StringIO()
    doc.write(buf)
    up = c.post(f'/admin/bending/tools/{tid}/profile/dxf', data={'file': (_io.BytesIO(buf.getvalue().encode()), 'p.dxf')}, content_type='multipart/form-data')
    pts_up = up.get_json()['points']
    assert up.status_code == 200 and max(p[1] for p in pts_up) == 67.09 and len(BendTool.query.filter_by(code='P1').one().profile) == 5  # tang cut off; review only, nothing saved
    assert c.post(f'/admin/bending/tools/{tid}/profile/dxf', data={'file': (_io.BytesIO(b'x'), 'p.txt')}, content_type='multipart/form-data').status_code == 400
    # outline endpoint: radii in, outline + radius list + numbers read off the contour + warnings against the catalogue data out
    import math as _m
    tt = _m.tan(_m.radians(37.5))
    good = [[0, 0, 0.8, 0], [6.2, 6.2 / tt, 0, 24.6], [6.2, 43.23, 0, 0], [6.2, 64, 0, 0], [-19.63, 64, 0, 0], [-19.63, 27.99, 0, 0], [-6.2, 6.2 / tt, 0, 0]]
    o = c.post(f'/admin/bending/tools/{tid}/profile/outline', json={'points': good}).get_json()
    assert abs(o['derived']['tip_angle'] - 75) < 0.05 and o['derived']['tip_radius'] == 0.8 and len(o['outline']) > len(good) and len(o['radii']) == 2
    assert o['warnings'] == []                       # matches the tool's own 75 deg / R0.8
    wrong = [[0, 0, 2.0, 0]] + good[1:]               # tip radius changed to R2 -> disagrees with the tool's R0.8
    assert any('R2' in w for w in c.post(f'/admin/bending/tools/{tid}/profile/outline', json={'points': wrong}).get_json()['warnings'])
    assert c.post(f'/admin/bending/tools/{tid}/profile/outline', json={'points': [[0, 0, 50, 0], [4, 0], [4, 4]]}).status_code == 400  # fillet does not fit
    # a die whose base is not the 60 mm of the die channel is flagged; shoulder radii are compared with the die's own radius
    from app import BendTool as _BT
    from bending.profile import default_profile as _dp
    db.session.add(_BT(code='D1', kind='die', angle_deg=85, radius_mm=2, v_opening_mm=16, height_mm=60))
    db.session.commit()
    did = _BT.query.filter_by(code='D1').one().id
    ok_die = c.post(f'/admin/bending/tools/{did}/profile/outline', json={'points': _dp('die', 85, 2, 60, 16), 'base_height_mm': 20}).get_json()
    assert ok_die['warnings'] == [] and ok_die['derived']['base_width'] == 60
    assert any('височина на основата' in w for w in c.post(f'/admin/bending/tools/{did}/profile/outline', json={'points': _dp('die', 85, 2, 60, 16)}).get_json()['warnings'])
    narrow = [[x * 50 / 60 if i not in (1, 2, 3) else x, y, r, a] for i, (x, y, r, a) in enumerate(_dp('die', 85, 2, 60, 16))]
    assert any('канал' in w for w in c.post(f'/admin/bending/tools/{did}/profile/outline', json={'points': narrow, 'base_height_mm': 20}).get_json()['warnings'])
    upper = [[-20, 0, 0, 0], [-8, 0, 2, 0], [0, -8.7, 0, 0], [8, 0, 2, 0], [20, 0, 0, 0], [20, -40, 0, 0], [30, -40, 0, 0], [30, -60, 0, 0], [-30, -60, 0, 0], [-30, -40, 0, 0], [-20, -40, 0, 0]]
    chk = lambda bh: c.post(f'/admin/bending/tools/{did}/profile/outline', json={'points': upper, 'base_height_mm': bh}).get_json()['warnings']
    assert not any('канал' in w for w in chk(20)) and any('канал' in w for w in chk(35))  # narrower above the base is fine; a taller base would include the narrow part
    assert c.post(f'/admin/bending/tools/{did}/profile', json={'points': upper, 'base_height_mm': 20}).status_code == 200 and _BT.query.filter_by(code='D1').one().base_height_mm == 20
    assert c.post(f'/admin/bending/tools/{did}/profile', json={'points': upper, 'base_height_mm': 99}).status_code == 400  # base taller than the die
    r3 = [[x, y, 3 if r else 0, a] for x, y, r, a in _dp('die', 85, 2, 60, 16)]
    assert any('R3' in w for w in c.post(f'/admin/bending/tools/{did}/profile/outline', json={'points': r3}).get_json()['warnings'])
    assert c.post(f'/admin/bending/tools/{tid}/profile', json={'points': [[0, 0, 5, 0], [4, 0], [4, 4]]}).status_code == 400
    assert c.post(f'/admin/bending/tools/{tid}/profile', json={'reset': True}).status_code == 200 and BendTool.query.filter_by(code='P1').one().profile is None


def test_multi_v_die_in_force_ranking_and_program_steps(ctx):
    import json as _json
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice
    from bending.profile import multi_v_die
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='M')
    db.session.add_all([User(username='bq_mv', password=generate_password_hash('irrelevant123'), role='admin'), m,
                        MaterialPrice(key='k', display_name='M', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add(PressBrakeConfig(machine=m, tonnage_t=175))
    d = Detail(name='D', material_key='k', width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    die = BendTool(code='M460.r', kind='die', angle_deg=85, height_mm=60, max_kn_per_m=800, status='in_library',
                   profile_json=_json.dumps(multi_v_die(60, [(50, 85, 3, 5), (16, 85, 2, 1), (35, 85, 2, 3), (22, 85, 2, 3)])))
    db.session.add_all([d, die])
    db.session.flush()
    p = BendProgram(detail=d, machine=m, name='P')
    db.session.add(p)
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_mv', 'password': 'irrelevant123'})
    # every V of the die is offered by the force calculator, named after the die
    j = c.get(f'/admin/bending/programs/{p.id}/force?rm=400&t=2&length=1000').get_json()
    assert sorted(x['code'] for x in j['dies']) == ['M460.r · V16', 'M460.r · V22', 'M460.r · V35', 'M460.r · V50']
    assert j['recommended_v'] == 12 and j['dies'][0]['code'] == 'M460.r · V16'   # closest to the recommended V12
    # the step form picks the die AND which V is up; it is stored and printed on the operator sheet
    page = c.get(f'/admin/bending/programs/{p.id}').get_data(as_text=True)
    assert f'value="{die.id}|16"' in page and 'M460.r · V35' in page
    c.post(f'/admin/bending/programs/{p.id}/steps/add', data={'angle_deg': '90', 'die_id': f'{die.id}|16'})
    st = BendStep.query.one()
    assert st.die_id == die.id and st.die_v_mm == 16
    assert 'M460.r · V16' in c.get(f'/admin/bending/programs/{p.id}/sheet').get_data(as_text=True)
    c.post(f'/admin/bending/programs/{p.id}/status', data={'status': 'approved'})
    c.post(f'/admin/bending/programs/{p.id}/new-version')
    assert BendStep.query.filter_by(die_v_mm=16).count() == 2   # a new version keeps which V was used
    # four grooves of one die: no single V / radius is compared with the catalogue numbers, but each angle is
    dd = c.get(f'/admin/bending/tools/{die.id}/profile/edit')
    assert dd.status_code == 200
    o = c.post(f'/admin/bending/tools/{die.id}/profile/outline', json={'points': _json.loads(die.profile_json), 'base_height_mm': 10}).get_json()
    assert len(o['derived']['grooves']) == 4 and o['warnings'] == []
    # a 20 mm base on the bottom side runs into the V22 groove on the left side (it starts ~11 mm above the bottom): flagged
    tall = c.post(f'/admin/bending/tools/{die.id}/profile/outline', json={'points': _json.loads(die.profile_json), 'base_height_mm': 20}).get_json()
    assert any('канала' in w or 'канал' in w for w in tall['warnings'])


def test_press_params_route(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Machine, PressBrakeConfig
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    db.session.add(User(username='pp_admin', password=generate_password_hash('irrelevant123'), role='admin'))
    m = Machine(name='AD-ES 2040')
    db.session.add(m)
    db.session.commit()
    from flask import g
    g.pop('_login_user', None)
    c = app.test_client()
    c.post('/login', data={'username': 'pp_admin', 'password': 'irrelevant123'})
    d = {'machine_id': m.id, 'tonnage_t': '40', 'working_length_mm': '2050', 'stroke_mm': '200', 'daylight_mm': '440', 'axes': 'X,R,Z1,Z2', 'controller': 'delem_da66t'}
    assert c.post('/admin/bending/press', data=d).status_code == 302
    d['stroke_mm'] = '210'
    c.post('/admin/bending/press', data=d)  # second save updates, not duplicates
    cfg = PressBrakeConfig.query.filter_by(machine_id=m.id).one()
    assert (cfg.tonnage_t, cfg.stroke_mm, cfg.daylight_mm, cfg.controller) == (40, 210, 440, 'delem_da66t')
    c.post('/admin/bending/press', data={**d, 'stroke_mm': '-5'})
    assert PressBrakeConfig.query.one().stroke_mm == 210  # invalid number refused
    assert c.get('/admin/bending').status_code == 200


def test_seed_press_brake_configs(ctx):
    from app import Machine, PressBrakeConfig
    from migration.seed_press_brake_configs import seed
    a, b = Machine(name='Абкант DURMA AD-R 40175'), Machine(name='Абкант DURMA AD-ES 2040')
    db.session.add_all([a, b])
    db.session.commit()
    b_cfg = PressBrakeConfig(machine=b, stroke_mm=210)
    db.session.add(b_cfg)
    db.session.commit()
    assert seed() == 2
    assert (a.press_brake.tonnage_t, a.press_brake.daylight_mm, a.press_brake.controller) == (175, 530, 'durma_dt15')
    assert (b.press_brake.stroke_mm, b.press_brake.tonnage_t) == (210, 40)  # existing value kept, gaps filled


def test_auto_tools(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice
    from migration.seed_bend_tools_m460r import seed
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    seed()
    db.session.add(User(username='bq_auto', password=generate_password_hash('irrelevant123'), role='admin'))
    m = Machine(name='Durma')
    db.session.add(m)
    db.session.add(MaterialPrice(key='mat2', display_name='M', type='sheets', cost_per_m2=1))
    db.session.flush()
    d = Detail(name='Скоба2', material_key='mat2', width=50, height=30, total_length=160, pierce_count=1, calculated_price=1, geometry_json='[]', thickness_mm=2)
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail_id=d.id, machine_id=m.id, name='P')
    db.session.add(p)
    db.session.flush()
    db.session.add(BendStep(program_id=p.id, seq=1, angle_deg=90))
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_auto', 'password': 'irrelevant123'})
    c.post(f'/admin/bending/programs/{p.id}/auto-tools')
    db.session.expire_all()
    st = p.steps[0]
    assert st.punch.code == 'P.97.75.r08' and st.die.code == 'M460.r' and st.die_v_mm == 16   # 2 mm -> V12 wanted, V16 is the nearest of 16/22/35/50
    assert c.get(f'/admin/bending/programs/{p.id}').status_code == 200


def test_program_steps_from_detail_dxf(ctx, tmp_path):
    import ezdxf
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, MaterialPrice, DetailDxfFile
    from migration.seed_bend_tools_m460r import seed
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, DETAIL_DXF_FOLDER=str(tmp_path))
    limiter.reset()
    seed()
    doc = ezdxf.new()
    doc.layers.add('BEND_UP')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 30), (0, 30)], close=True, dxfattribs={'layer': 'CUT'})
    msp.add_line((30, 0), (30, 30), dxfattribs={'layer': 'BEND_UP'})
    msp.add_line((70, 0), (70, 30), dxfattribs={'layer': 'BEND_UP_45'})
    doc.saveas(str(tmp_path / 'd.dxf'))
    db.session.add(User(username='bq_gen', password=generate_password_hash('irrelevant123'), role='admin'))
    m = Machine(name='Durma3')
    db.session.add(m)
    db.session.add(MaterialPrice(key='mat3', display_name='M', type='sheets', cost_per_m2=1))
    db.session.flush()
    d = Detail(name='Скоба3', material_key='mat3', width=100, height=30, total_length=160, pierce_count=1, calculated_price=1, geometry_json='[]', thickness_mm=2)
    db.session.add(d)
    db.session.flush()
    db.session.add(DetailDxfFile(detail_id=d.id, filename='d.dxf', original_filename='d.dxf'))
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_gen', 'password': 'irrelevant123'})
    c.post('/admin/bending/programs/add', data={'detail_id': d.id, 'machine_id': m.id})
    prog = BendProgram.query.one()
    assert [(s.angle_deg, s.x_mm) for s in prog.steps] == [(90, 30), (45, 30)]
    assert all(s.punch and s.die for s in prog.steps)  # thickness 2 -> tools picked for both
    r = c.get(f'/bending/steps/{prog.steps[0].id}/picture.svg')
    assert r.status_code == 200 and r.mimetype == 'image/svg+xml' and 'упор' in r.get_data(as_text=True) and 'X 30' in r.get_data(as_text=True)
    v = c.get(f'/bending/steps/{prog.steps[0].id}/view')
    assert v.status_code == 200 and b'id="stage"' in v.data and b'<svg' in v.data
    sim = c.get(f'/bending/programs/{prog.id}/sim')
    assert sim.status_code == 200 and b'id="simdata"' in sim.data and b'"frames"' in sim.data
    n = len(prog.steps)
    assert c.post(f'/admin/bending/programs/{prog.id}/plan').status_code == 302   # re-plans from the DXF: same bends, tools chosen
    db.session.expire_all()
    assert len(prog.steps) == n and all(x.punch and x.die and x.back_end for x in prog.steps)
    assert c.get(f'/admin/bending/programs/{prog.id}').status_code == 200 and c.get(f'/admin/bending/programs/{prog.id}/sheet').status_code == 200


def test_step_picture_shows_whole_part_without_dxf(ctx):
    import re
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice, BendTool
    from migration.seed_bend_tools_m460r import seed
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    seed()
    db.session.add(User(username='bq_pic', password=generate_password_hash('irrelevant123'), role='admin'))
    m = Machine(name='Durma4')
    db.session.add(m)
    db.session.add(MaterialPrice(key='mat4', display_name='M', type='sheets', cost_per_m2=1))
    db.session.flush()
    d = Detail(name='Z', material_key='mat4', width=100, height=30, total_length=160, pierce_count=1, calculated_price=1, geometry_json='[]', thickness_mm=2)
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail_id=d.id, machine_id=m.id, name='Z')
    db.session.add(p)
    db.session.flush()
    punch, die = BendTool.query.filter_by(code='P.97.75.r08').one(), BendTool.query.filter_by(code='M460.r').one()
    for i, (x, flip) in enumerate([(80, False), (40, True)], 1):   # old style steps: no back_end / bend_dir
        db.session.add(BendStep(program_id=p.id, seq=i, angle_deg=90, x_mm=x, flip=flip, punch_id=punch.id, die_id=die.id, die_v_mm=16))
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_pic', 'password': 'irrelevant123'})
    svg = c.get(f'/bending/steps/{p.steps[1].id}/picture.svg').get_data(as_text=True)
    sheet = [m_ for m_ in re.findall(r'<path d="(M [^"]*)" stroke="#e26a4a" stroke-width="2"[^>]*stroke-linejoin', svg)]
    assert sheet and sheet[-1].count('L') == 3   # back leg + apex + front leg + the bent flange of step 1


def test_change_program_machine(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, MaterialPrice
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    db.session.add_all([User(username='bq_mc', password=generate_password_hash('irrelevant123'), role='admin'), Machine(name='M1'), Machine(name='M2'),
                        MaterialPrice(key='k', display_name='M', type='sheets', cost_per_m2=1)])
    db.session.flush()
    m1, m2 = Machine.query.filter_by(name='M1').one(), Machine.query.filter_by(name='M2').one()
    db.session.add_all([PressBrakeConfig(machine=m1, working_length_mm=2050), PressBrakeConfig(machine=m2, working_length_mm=4050)])
    d = Detail(name='D', material_key='k', width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=m1, name='P')
    db.session.add(p)
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_mc', 'password': 'irrelevant123'})
    assert b'M2' in c.get(f'/admin/bending/programs/{p.id}').data                       # offered in the program's machine select
    c.post(f'/admin/bending/programs/{p.id}/machine', data={'machine_id': m2.id})
    assert db.session.get(BendProgram, p.id).machine_id == m2.id
    c.post(f'/admin/bending/programs/{p.id}/machine', data={'machine_id': 9999})        # unknown machine: unchanged
    assert db.session.get(BendProgram, p.id).machine_id == m2.id
    p.status = 'approved'
    db.session.commit()
    c.post(f'/admin/bending/programs/{p.id}/machine', data={'machine_id': m1.id})       # an approved program keeps its machine
    assert db.session.get(BendProgram, p.id).machine_id == m2.id


def test_program_time_operation(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice, Service, Operation
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='MT')
    sv = Service(name='Огъване', price_per_hour_eur=60.0, machines=[m])
    other = Service(name='Друга', price_per_hour_eur=10.0)
    db.session.add_all([User(username='bq_tm', password=generate_password_hash('irrelevant123'), role='admin'), m, sv, other,
                        MaterialPrice(key='k', display_name='M', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add(PressBrakeConfig(machine=m, working_length_mm=2050))
    d = Detail(name='D', material_key='k', width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=m, name='P', steps=[BendStep(seq=1, angle_deg=90), BendStep(seq=2, angle_deg=90, flip=True)])
    db.session.add(p)
    db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'bq_tm', 'password': 'irrelevant123'})
    assert 'Време и цена'.encode() in c.get(f'/admin/bending/programs/{p.id}').data
    c.post(f'/admin/bending/programs/{p.id}/operation', data={'service_id': other.id})   # not the machine's service: refused
    assert Operation.query.count() == 0
    c.post(f'/admin/bending/programs/{p.id}/operation', data={'service_id': sv.id})      # 2*25 + 10 + 20 s = 80 s
    c.post(f'/admin/bending/programs/{p.id}/operation', data={'service_id': sv.id})      # again: updated, not duplicated
    ops = Operation.query.all()
    assert len(ops) == 1 and ops[0].duration_minutes == round(80 / 60, 2) and ops[0].service_id == sv.id


def test_chatbot_bending_tools_staff_only(ctx):
    from flask_login import login_user
    from werkzeug.security import generate_password_hash
    from app import User, Detail, BendProgram, BendStep, BendTool, BendToolSegment, MaterialPrice, bending_program_info, bend_tool_stock
    m = Machine(name='MC')
    staff = User(username='bq_cs', password=generate_password_hash('irrelevant123'), role='worker')
    client = User(username='bq_cc', password=generate_password_hash('irrelevant123'), role='regular_user')
    d = Detail(name='Конзола X', material_key='k', width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    tool = BendTool(code='P.97.test', kind='punch')
    db.session.add_all([m, staff, client, d, tool, MaterialPrice(key='k', display_name='M', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add_all([PressBrakeConfig(machine=m, working_length_mm=2050), BendToolSegment(tool_id=tool.id, length_mm=500),
                        BendProgram(detail=d, machine=m, name='P', steps=[BendStep(seq=1, angle_deg=90)])])
    db.session.commit()
    with app.test_request_context():
        login_user(client)
        assert 'само за служители' in bending_program_info.call({'detail_name': 'Конзола'}) and 'само за служители' in bend_tool_stock.call({'query': 'P.97'})
        login_user(staff)
        assert '1 огъвания' in bending_program_info.call({'detail_name': 'Конзола'})
        assert '500 mm (на склад)' in bend_tool_stock.call({'query': 'P.97'})


def test_springback_is_learned_from_measured_angles(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice, BendSpringbackCorrection, _overbend_fn
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='MS')
    die = BendTool(code='D16', kind='die', v_opening_mm=16)
    db.session.add_all([User(username='bq_sb', password=generate_password_hash('irrelevant123'), role='admin'), m, die, MaterialPrice(key='st', display_name='Стомана', type='sheets', cost_per_m2=1)])
    db.session.flush()
    d = Detail(name='D', material_key='st', thickness_mm=2.0, width=1, height=1, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=m, name='P', status='approved', steps=[BendStep(seq=1, angle_deg=90, die_id=die.id), BendStep(seq=2, angle_deg=90, die_id=die.id)])
    db.session.add(p)
    db.session.commit()
    base = _overbend_fn(p)(16, 2.0)
    c = app.test_client()
    c.post('/login', data={'username': 'bq_sb', 'password': 'irrelevant123'})
    s1, s2 = p.steps
    c.post(f'/bending/programs/{p.id}/execute', data={f'm{s1.id}': '92', f'm{s2.id}': '95'})   # 2 and 5 degrees too open, V16 x 2 mm
    row = BendSpringbackCorrection.query.one()
    assert (row.v_mm, row.thickness_mm, row.samples) == (16.0, 2.0, 2) and row.correction_deg == pytest.approx(3.5, abs=0.01)   # 0 + 2/2, then + 5/2
    assert _overbend_fn(p)(16, 2.0) == round(base + row.correction_deg, 1)
    c.post(f'/bending/programs/{p.id}/execute', data={f'm{s1.id}': '130'})    # a 40 degree "error" is a typo: ignored
    assert BendSpringbackCorrection.query.one().samples == 2
    assert b'+3.5' in c.get('/admin/bending').data
    c.post(f'/admin/bending/springback/{row.id}/delete')
    assert BendSpringbackCorrection.query.count() == 0


def test_missing_set_suggests_nearest_and_alternative_tools(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice, _program_tooling
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='MN')
    punch = BendTool(code='P', kind='punch', status='in_library', angle_deg=88)
    die_a = BendTool(code='DA', kind='die', status='in_library', v_opening_mm=16, angle_deg=88)
    die_b = BendTool(code='DB', kind='die', status='in_library', v_opening_mm=14, angle_deg=88)   # a full set exists only for this one
    db.session.add_all([User(username='bq_ns', password=generate_password_hash('irrelevant123'), role='admin'), m, punch, die_a, die_b,
                        MaterialPrice(key='st', display_name='Стомана', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add(PressBrakeConfig(machine=m, working_length_mm=2050))
    for tool, pieces in ((punch, [100, 40, 40, 15, 200]), (die_a, [100, 40, 40, 15]), (die_b, [200, 100, 40, 40, 15])):
        db.session.add_all([BendToolSegment(tool=tool, length_mm=l, erp_number=None) for l in pieces])
    d = Detail(name='D', material_key='st', thickness_mm=2.0, width=300, height=100, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=m, name='P', steps=[BendStep(seq=1, angle_deg=90, punch_id=punch.id, die_id=die_a.id, length_mm=300)])
    db.session.add(p)
    db.session.commit()
    (su,) = _program_tooling(p)['setups']
    (sg,) = su['suggest']
    assert su['missing'] == [320] and sg['part'] == 300 and sg['near']['short']['total'] == 195 and sum(sg['near']['buy']['add']) == 125   # 300 + 10 mm at each end
    assert [(a['die'], a['v']) for a in sg['alts']] == [('DB', 14.0)]
    c = app.test_client()
    c.post('/login', data={'username': 'bq_ns', 'password': 'irrelevant123'})
    html = c.get(f'/admin/bending/programs/{p.id}').get_data(as_text=True)
    assert 'Най-близкият комплект' in html and 'Друг инструмент с пълен комплект' in html and 'Попитай AI' in html
    c.post(f'/admin/bending/programs/{p.id}/retool', data={'punch_id': punch.id, 'die_id': die_b.id, 'steps': '1', 'v': '14'})
    db.session.expire_all()
    assert p.steps[0].die_id == die_b.id and not _program_tooling(p)['setups'][0]['missing']
    r = c.post(f'/admin/bending/programs/{p.id}/ai-tooling', json={'setup': 1, 'need': 320})   # nothing missing any more
    assert r.status_code in (400, 503)


def test_tool_is_longer_than_the_bend_by_a_margin(ctx):
    from app import Detail, BendProgram, BendStep, MaterialPrice, _program_tooling
    m = Machine(name='MM')
    punch, die = BendTool(code='P', kind='punch', status='in_library'), BendTool(code='D', kind='die', status='in_library', v_opening_mm=16)
    db.session.add_all([m, punch, die, MaterialPrice(key='st', display_name='S', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add(PressBrakeConfig(machine=m, working_length_mm=2050))
    db.session.add_all([BendToolSegment(tool=t, length_mm=l) for t in (punch, die) for l in (100, 100, 100)])
    d = Detail(name='D', material_key='st', thickness_mm=2.0, width=300, height=100, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    p = BendProgram(detail=d, machine=m, name='P', steps=[BendStep(seq=1, angle_deg=90, punch=punch, die=die, length_mm=290), BendStep(seq=2, angle_deg=90, punch=punch, die=die, length_mm=300)])
    db.session.add(p)
    db.session.commit()
    (su,) = _program_tooling(p)['setups']
    assert su['missing'] == [310, 320]   # 290 and 300 mm bends + 10 mm at each end: both need more than the 300 mm in stock
    m.press_brake.kinematics_json = '{"tool_margin_mm": 0}'
    db.session.commit()
    (su,) = _program_tooling(p)['setups']
    assert su['missing'] == [] and su['windows'][-1]['part'] == 300 and su['total'] == 300      # without a margin the old exact cover is back


def test_part_views_and_model_radius(ctx, tmp_path):
    import json
    import ezdxf
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, DetailDxfFile, BendProgram, MaterialPrice
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='MV')
    doc = ezdxf.new('R2000')
    msp = doc.modelspace()
    for a, b in (((0, 0), (80, 0)), ((80, 0), (80, 50)), ((80, 50), (0, 50)), ((0, 50), (0, 0))):
        msp.add_line(a, b, dxfattribs={'layer': 'CUT'})
    msp.add_circle((20, 25), 4, dxfattribs={'layer': 'CUT'})
    msp.add_line((50, 0), (50, 50), dxfattribs={'layer': 'BEND_DOWN_90_R3'})
    name = 'pv_test_part.dxf'
    path = os.path.join(app.config['DETAIL_DXF_FOLDER'], name)
    doc.saveas(path)
    try:
        d = Detail(name='D', material_key='st', thickness_mm=2.0, width=80, height=50, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
        db.session.add_all([User(username='bq_pv', password=generate_password_hash('irrelevant123'), role='admin'), m, d, MaterialPrice(key='st', display_name='S', type='sheets', cost_per_m2=1)])
        db.session.flush()
        db.session.add_all([PressBrakeConfig(machine=m, working_length_mm=2050), DetailDxfFile(detail=d, filename=name, original_filename=name)])
        p = BendProgram(detail=d, machine=m, name='P')
        db.session.add(p)
        db.session.commit()
        c = app.test_client()
        c.post('/login', data={'username': 'bq_pv', 'password': 'irrelevant123'})
        c.post(f'/admin/bending/programs/{p.id}/generate')
        db.session.expire_all()
        assert [s.inner_radius_mm for s in BendProgram.query.one().steps] == [3.0]          # the radius of the model reached the step
        for url, interactive in ((f'/admin/bending/programs/{p.id}', True), (f'/admin/bending/programs/{p.id}/sheet', False)):
            html = c.get(url).get_data(as_text=True)
            assert 'id="partViewData"' in html and 'Детайл: разгънат и сгънат' in html and ('id="partFold"' in html) == interactive
            data = json.loads(html.split('id="partViewData">')[1].split('</script>')[0])
            assert data['L'] == 80 and data['thickness'] == 2.0 and len(data['loops']) == 2 and data['loops'][0] and len(data['loops'][0]) == 4   # the outline + the hole
            assert [(b['pos'], b['angle'], b['dir'], b['radius']) for b in data['bends']] == [(50.0, 90.0, 'down', 3.0)]
            assert '<svg' in html and 'stroke-dasharray' in html
            assert 'Y1 = Y2' in html and '>Z1<' in html and '>Z2<' in html and 'Ъгъл' in html   # every axis of the step is shown for typing it in by hand (the sheet and the program page)
    finally:
        os.remove(path)


def test_plan_uses_tools_in_stock_first_then_the_library(ctx):
    import json
    import ezdxf
    import migration.seed_bend_tools_m460r as seed
    from bending.profile import multi_v_die, tip_offset
    from app import User, limiter, Detail, DetailDxfFile, BendProgram, MaterialPrice, _plan_program
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    off = tip_offset(75, 0.8)
    punch_pts = [[x, y - off, r, a] for x, y, r, a in seed.P97_PROFILE]
    die_pts = multi_v_die(seed.DIE_SIZE, seed.M460R_SIDES)
    m = Machine(name='MP')
    pa, pb = (BendTool(code=c, kind='punch', status='in_library', angle_deg=75, radius_mm=0.8, profile_json=json.dumps(punch_pts)) for c in ('PA', 'PB'))
    da, db_ = (BendTool(code=c, kind='die', status='in_library', angle_deg=85, v_opening_mm=16, height_mm=60, profile_json=json.dumps(die_pts)) for c in ('DA', 'DB'))
    doc = ezdxf.new('R2000')
    msp = doc.modelspace()
    for a, b in (((0, 0), (100, 0)), ((100, 0), (100, 50)), ((100, 50), (0, 50)), ((0, 50), (0, 0))):
        msp.add_line(a, b, dxfattribs={'layer': 'CUT'})
    msp.add_line((30, 0), (30, 50), dxfattribs={'layer': 'BEND_UP_90'})
    name = 'plan_stock_test.dxf'
    path = os.path.join(app.config['DETAIL_DXF_FOLDER'], name)
    doc.saveas(path)
    try:
        d = Detail(name='D', material_key='st', thickness_mm=2.0, width=100, height=50, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
        db.session.add_all([m, pa, pb, da, db_, d, MaterialPrice(key='st', display_name='S', type='sheets', cost_per_m2=1)])
        db.session.flush()
        db.session.add_all([PressBrakeConfig(machine=m, working_length_mm=2050), DetailDxfFile(detail=d, filename=name, original_filename=name)])
        p = BendProgram(detail=d, machine=m, name='P')
        db.session.add(p)
        db.session.commit()
        # nothing in stock: no tool has a set - the whole library is searched and the plan says so
        steps, why, source = _plan_program(p)
        assert steps and source == 'library'
        # pieces for PB + DB only: the plan is made from them, even though PA/DA stand first in the library
        db.session.add_all([BendToolSegment(tool=t, length_mm=l) for t in (pb, db_) for l in (100, 40, 15)])
        db.session.commit()
        steps, why, source = _plan_program(p)
        assert source == 'stock' and (steps[0]['punch_id'], steps[0]['die_id']) == (pb.id, db_.id)
    finally:
        os.remove(path)


def test_tapered_part_has_slanted_bend_lines_and_x1_x2(ctx):
    import json
    import ezdxf
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, DetailDxfFile, BendProgram, MaterialPrice, _program_checks, _program_tooling
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    m = Machine(name='MT2')
    doc = ezdxf.new('R2000')
    msp = doc.modelspace()
    # a cladding that narrows from 300 to 240 over 1000 mm: the right edge and the bend line (c = 60 -> 30) are slanted
    msp.add_lwpolyline([(0, 0), (300, 0), (240, 1000), (0, 1000)], close=True, dxfattribs={'layer': 'CUT'})
    msp.add_line((60, 0), (30, 1000), dxfattribs={'layer': 'BEND_UP_90_R2'})
    name = 'tapered_test.dxf'
    path = os.path.join(app.config['DETAIL_DXF_FOLDER'], name)
    doc.saveas(path)
    try:
        d = Detail(name='D', material_key='st', thickness_mm=2.0, width=300, height=1000, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
        db.session.add_all([User(username='bq_tp', password=generate_password_hash('irrelevant123'), role='admin'), m, d, MaterialPrice(key='st', display_name='S', type='sheets', cost_per_m2=1)])
        db.session.flush()
        db.session.add_all([PressBrakeConfig(machine=m, working_length_mm=2050), DetailDxfFile(detail=d, filename=name, original_filename=name)])
        p = BendProgram(detail=d, machine=m, name='P')
        db.session.add(p)
        db.session.commit()
        c = app.test_client()
        c.post('/login', data={'username': 'bq_tp', 'password': 'irrelevant123'})
        c.post(f'/admin/bending/programs/{p.id}/generate')
        db.session.expire_all()
        st = BendProgram.query.one().steps[0]
        assert json.loads(st.line_json) == [60.0, -500.0, 30.0, 500.0] and st.inner_radius_mm == 2.0   # the real, slanted line and the radius of the generator
        ax = _program_checks(p, _program_tooling(p))[st.id]['axes']
        assert ax['x1'] is not None and ax['x2'] is not None and abs(ax['x1'] - ax['x2']) > 0.5   # X differs between the two fingers
        html = c.get(f'/admin/bending/programs/{p.id}/sheet').get_data(as_text=True)
        assert '>X1<' in html and '>X2<' in html
        data = json.loads(html.split('id="partViewData">')[1].split('</script>')[0])
        assert data['bends'][0]['line'] == [60.0, -500.0, 30.0, 500.0]                       # the flat view draws the line as it is, slanted
    finally:
        os.remove(path)


def test_part_too_big_for_the_machine_is_refused_and_another_is_offered(ctx):
    from werkzeug.security import generate_password_hash
    from app import User, limiter, Detail, BendProgram, BendStep, MaterialPrice, _machine_fit
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    small, big, tiny = Machine(name='Малък 2050'), Machine(name='Голям 4050'), Machine(name='Съвсем малък')
    db.session.add_all([User(username='bq_fit', password=generate_password_hash('irrelevant123'), role='admin'), small, big, tiny, MaterialPrice(key='st', display_name='S', type='sheets', cost_per_m2=1)])
    db.session.flush()
    db.session.add_all([PressBrakeConfig(machine=small, working_length_mm=2050, tonnage_t=40, x_max_mm=650), PressBrakeConfig(machine=big, working_length_mm=4050, tonnage_t=175, x_max_mm=800),
                        PressBrakeConfig(machine=tiny, working_length_mm=1250, tonnage_t=30, x_max_mm=500)])
    d = Detail(name='D', material_key='st', thickness_mm=2.0, width=3000, height=500, total_length=1, pierce_count=1, calculated_price=1, geometry_json='[]')
    db.session.add(d)
    db.session.flush()
    p = BendProgram(detail=d, machine=small, name='P', steps=[BendStep(seq=1, angle_deg=90, x_mm=100, length_mm=3000)])
    db.session.add(p)
    db.session.commit()
    fit = _machine_fit(p)
    assert not fit['ok'] and fit['problems'][0]['kind'] == 'length' and [a['machine'].name for a in fit['alternatives']] == ['Голям 4050']   # the one that can take it
    c = app.test_client()
    c.post('/login', data={'username': 'bq_fit', 'password': 'irrelevant123'})
    html = c.get(f'/admin/bending/programs/{p.id}').get_data(as_text=True)
    assert 'НЕ МОЖЕ ДА СЕ ИЗПЪЛНИ НА ТАЗИ МАШИНА' in html and 'Голям 4050' in html and 'Смени машината' in html
    assert 'НЕ МОЖЕ ДА СЕ ИЗПЪЛНИ' in c.get(f'/admin/bending/programs/{p.id}/sheet').get_data(as_text=True)
    c.post(f'/admin/bending/programs/{p.id}/status', data={'status': 'approved'})        # refused
    db.session.expire_all()
    assert BendProgram.query.one().status == 'draft'
    c.post(f'/admin/bending/programs/{p.id}/machine', data={'machine_id': big.id})        # switch to the offered machine
    db.session.expire_all()
    assert _machine_fit(BendProgram.query.one())['ok']
    r = c.post(f'/admin/bending/programs/{p.id}/status', data={'status': 'approved'}, follow_redirects=True)
    db.session.expire_all()
    import re as _re
    assert BendProgram.query.one().status == 'approved', _re.findall(r'alert alert-[a-z]+">([^<]+)', r.get_data(as_text=True)) or r.status_code
    # a part nobody can take: no alternative, and the banner says what is needed
    p2 = BendProgram(detail=d, machine=big, name='P2', steps=[BendStep(seq=1, angle_deg=90, length_mm=6000)])
    db.session.add(p2)
    db.session.commit()
    f2 = _machine_fit(p2)
    assert not f2['ok'] and not f2['alternatives']
    assert 'няма абкант' in c.get(f'/admin/bending/programs/{p2.id}').get_data(as_text=True)
    # force: a thick plate over a short V needs more than the small machine can give
    p3 = BendProgram(detail=d, machine=small, name='P3', steps=[BendStep(seq=1, angle_deg=90, length_mm=2000, force_kn=900)])
    db.session.add(p3)
    db.session.commit()
    f3 = _machine_fit(p3)
    assert [x['kind'] for x in f3['problems']] == ['force'] and [a['machine'].name for a in f3['alternatives']] == ['Голям 4050']
