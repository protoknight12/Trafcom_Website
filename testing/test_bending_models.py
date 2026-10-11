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
    assert seed() == (5, 120)
    assert seed() == (0, 0)  # idempotent
    t = BendTool.query.filter_by(code='M460.r V50').one()
    assert (t.v_opening_mm, t.radius_mm, t.angle_deg, t.max_kn_per_m, t.status) == (50, 3, 85, 800, 'in_library')
    assert sorted(s.length_mm for s in t.segments) == sorted([10, 15, 20, 40, 50, 100, 200, 370] * 3)
    erps = [s.erp_number for s in BendToolSegment.query.all()]
    assert len(set(erps)) == 120
    p = BendTool.query.filter_by(code='P.97.75.r08').one()
    assert (p.kind, p.height_mm, p.angle_deg, p.radius_mm, round(p.max_kn_per_m, 1), len(p.segments)) == ('punch', 67.6, 75, 0.8, 980.7, 24)
