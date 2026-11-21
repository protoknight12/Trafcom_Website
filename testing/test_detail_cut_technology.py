"""
pytest: cutting technology per detail and machine (owner's decision 2026-11-20). A new detail copies its material's
speed / pierce time; the detail page saves its own machine + speeds and, on request, the machine's technology for the
material's Вид + thickness (one row, updated in place); the page offers that technology for the machine; admin only.

Run with:
    pytest testing/test_detail_cut_technology.py -v
"""
import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, MaterialGrade, Detail, Machine, MachineCutTechnology,
                 limiter, seed_material_types)


@pytest.fixture
def s():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        grade = MaterialGrade(name='Стомана QA', density_kg_dm3=7.85)
        db.session.add_all([User(username='ct_admin', password=pw, role='admin'),
                            User(username='ct_worker', password=pw, role='worker'), grade,
                            Machine(name='Лазер 1', is_laser=True), Machine(name='Лазер 2', is_laser=True),
                            Machine(name='Струг QA')])
        db.session.flush()
        db.session.add(MaterialPrice(key='ct_steel', display_name='Стомана 2', type='sheets', cost_per_m2=10,
                                     sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2, grade_id=grade.id,
                                     cutting_speed_mm_per_min=1000, pierce_rate_per_min=60))
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_detail_cut_technology(s):
    c = _login('ct_admin')
    c.post('/admin/details/add', data={'name': 'Планка', 'material': 'ct_steel', 'manual_price': '1'})
    d = Detail.query.filter_by(name='Планка').one()
    assert (d.cutting_speed_mm_per_min, d.pierce_time_sec) == (1000, 1.0)        # copied from the material

    m1, m2 = Machine.query.filter_by(is_laser=True).order_by(Machine.id).all()
    url = f'/admin/details/{d.id}/cut-technology'
    c.post(url, data={'machine_id': m1.id, 'cutting_speed_mm_per_min': '2500', 'pierce_time_sec': '0,5',
                      'save_to_machine': '1'})
    db.session.expire_all()
    d = db.session.get(Detail, d.id)
    assert (d.cutting_machine_id, d.cutting_speed_mm_per_min, d.pierce_time_sec) == (m1.id, 2500, 0.5)
    tech = MachineCutTechnology.query.one()
    assert (tech.machine_id, tech.thickness_mm, tech.cutting_speed_mm_per_min, tech.pierce_time_sec) == (m1.id, 2, 2500, 0.5)
    assert tech.grade_id == d.material.grade_id
    assert MaterialPrice.query.filter_by(key='ct_steel').one().cutting_speed_mm_per_min == 1000   # material untouched

    c.post(url, data={'machine_id': m1.id, 'cutting_speed_mm_per_min': '2600', 'pierce_time_sec': '0.4',
                      'save_to_machine': '1'})                                       # same key: updated, not added
    db.session.expire_all()
    assert MachineCutTechnology.query.count() == 1 and MachineCutTechnology.query.one().cutting_speed_mm_per_min == 2600

    c.post(url, data={'machine_id': m1.id, 'gas': 'nitrogen', 'cutting_speed_mm_per_min': '3100',
                      'save_to_machine': '1'})                                       # another gas = another row
    db.session.expire_all()
    assert db.session.get(Detail, d.id).cutting_gas == 'nitrogen'
    assert sorted((t.gas or '', t.cutting_speed_mm_per_min) for t in MachineCutTechnology.query) == \
           [('', 2600), ('nitrogen', 3100)]
    page = c.get(f'/details/{d.id}/files').get_data(as_text=True)
    import json, re
    tech_js = json.loads(re.search(r'const TECH = (.*?);\n', page).group(1))
    # per machine, first recorded first: picking the machine fills the first, the rest are listed under the form
    assert [(t['gas'], t['speed']) for t in tech_js[str(m1.id)]] == [('', 2600), ('nitrogen', 3100)]
    assert 'id="ct_gas"' in page and 'id="ct_others"' in page
    MachineCutTechnology.query.filter_by(gas='nitrogen').delete()
    db.session.commit()

    c.post(url, data={'machine_id': m2.id, 'cutting_speed_mm_per_min': '900', 'pierce_time_sec': ''})
    db.session.expire_all()
    assert MachineCutTechnology.query.count() == 1                                   # not asked to save
    assert (db.session.get(Detail, d.id).cutting_machine_id, db.session.get(Detail, d.id).pierce_time_sec) == (m2.id, None)

    page = c.get(f'/details/{d.id}/files').get_data(as_text=True)
    assert 'id="ct_machine"' in page and '"speed": 2600' in page.replace('2600.0', '2600')
    assert 'Струг QA' not in page                                                    # only laser machines offered
    assert 'const OP_CUTTING_SPEED_MM_PER_MIN = 900' in page.replace('900.0', '900')

    c.post(url, data={'machine_id': m1.id, 'cutting_speed_mm_per_min': '-5'})       # refused
    db.session.expire_all()
    assert db.session.get(Detail, d.id).cutting_speed_mm_per_min == 900

    _login('ct_worker').post(url, data={'machine_id': m1.id, 'cutting_speed_mm_per_min': '1'})
    db.session.expire_all()
    assert db.session.get(Detail, d.id).cutting_speed_mm_per_min == 900

    c = _login('ct_admin')
    c.post(f'/machines/{m1.id}/delete')
    db.session.expire_all()
    assert MachineCutTechnology.query.count() == 0


def test_non_sheet_detail_only_picks_a_machine(s):
    """A detail not cut from sheet: every machine is offered, no speeds / gas / technology, saving keeps only the
    machine; a sheet detail refuses a non-laser machine."""
    c = _login('ct_admin')
    db.session.add(MaterialPrice(key='ct_rod', display_name='Прът 20', type='rods', cost_per_m2=5,
                                 sheet_width_mm=20, sheet_length_mm=6000))
    db.session.commit()
    c.post('/admin/details/add', data={'name': 'Ос', 'material': 'ct_rod', 'manual_price': '1'})
    d = Detail.query.filter_by(name='Ос').one()
    assert not d.laser_cut
    lathe = Machine.query.filter_by(name='Струг QA').one()
    page = c.get(f'/details/{d.id}/files').get_data(as_text=True)
    assert 'Струг QA' in page and 'id="ct_speed"' not in page and 'id="ct_gas"' not in page
    c.post(f'/admin/details/{d.id}/cut-technology', data={'machine_id': lathe.id, 'cutting_speed_mm_per_min': '99',
                                                          'save_to_machine': '1'})
    db.session.expire_all()
    d = db.session.get(Detail, d.id)
    assert d.cutting_machine_id == lathe.id and d.cutting_speed_mm_per_min != 99
    assert MachineCutTechnology.query.count() == 0

    c.post('/admin/details/add', data={'name': 'Лист', 'material': 'ct_steel', 'manual_price': '1'})
    sheet = Detail.query.filter_by(name='Лист').one()
    assert sheet.laser_cut
    assert 'Струг QA' not in c.get(f'/details/{sheet.id}/files').get_data(as_text=True)
    c.post(f'/admin/details/{sheet.id}/cut-technology', data={'machine_id': lathe.id})
    db.session.expire_all()
    assert db.session.get(Detail, sheet.id).cutting_machine_id is None


def test_dossier_cut_technologies(s):
    """The laser flag in the dossier, and add / edit / duplicate / delete of a machine's technologies there."""
    from app import HallMachine
    c = _login('ct_admin')
    m = Machine.query.filter_by(name='Струг QA').one()
    hm = HallMachine(name=m.name, machine=m)
    db.session.add(hm)
    db.session.commit()
    grade = MaterialGrade.query.filter_by(name='Стомана QA').one()
    url = f'/admin/hall/{hm.id}/dossier'
    assert 'Технологии на рязане' not in c.get(url).get_data(as_text=True)          # not a laser yet

    c.post(f'{url}/system', data={'status': 'idle', 'is_laser': '1'})
    db.session.expire_all()
    assert db.session.get(Machine, m.id).is_laser
    assert 'Технологии на рязане' in c.get(url).get_data(as_text=True)

    base = f'/admin/hall/{hm.id}/cut-tech'
    c.post(base, data={'grade_id': grade.id, 'thickness_mm': '3', 'cutting_speed_mm_per_min': '1800',
                       'pierce_time_sec': '0.8'})
    c.post(base, data={'grade_id': grade.id, 'thickness_mm': '3', 'cutting_speed_mm_per_min': '1'})   # duplicate
    c.post(base, data={'grade_id': '', 'thickness_mm': '1', 'cutting_speed_mm_per_min': '5000'})      # без вид
    c.post(base, data={'grade_id': grade.id, 'thickness_mm': '3', 'gas': 'oxygen',
                       'cutting_speed_mm_per_min': '1200'})                          # same Вид/thickness, other gas
    db.session.expire_all()
    ox = MachineCutTechnology.query.filter_by(gas='oxygen').one()
    assert ox.cutting_speed_mm_per_min == 1200
    c.post(f'{base}/{ox.id}/delete')
    db.session.expire_all()
    rows = MachineCutTechnology.query.order_by(MachineCutTechnology.thickness_mm).all()
    assert [(r.grade_id, r.thickness_mm, r.cutting_speed_mm_per_min) for r in rows] == \
           [(None, 1, 5000), (grade.id, 3, 1800)]
    t = rows[1]
    c.post(base, data={'tech_id': t.id, 'grade_id': grade.id, 'thickness_mm': '4', 'cutting_speed_mm_per_min': '1500',
                       'pierce_time_sec': ''})
    db.session.expire_all()
    t = db.session.get(MachineCutTechnology, t.id)
    assert (t.thickness_mm, t.cutting_speed_mm_per_min, t.pierce_time_sec) == (4, 1500, None)
    c.post(f'{base}/{t.id}/delete')
    db.session.expire_all()
    assert MachineCutTechnology.query.count() == 1
