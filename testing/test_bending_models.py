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
