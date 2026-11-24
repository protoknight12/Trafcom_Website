"""
pytest: MachineCutTechnology.needs_test ("За тест"), its "Работи ✓" route and the PG3015 proposal data step.

Run with:
    pytest testing/test_cut_tech_needs_test.py -v
"""
import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Machine, HallMachine, MaterialGrade, MachineCutTechnology,
                 MachineThicknessRange, limiter)
from migration.data_pg3015_proposed_tech import apply, PROPOSALS

GRADES = ('Стомана', 'Неръждаема стомана', 'Алуминий', 'Месинг', 'Поцинкована стомана', 'Мед')


class _Client:
    def __init__(self, client):
        self._c = client

    def __getattr__(self, name):
        f = getattr(self._c, name)
        if name not in ('get', 'post'):
            return f

        def call(*a, **kw):
            g.pop('_login_user', None)
            return f(*a, **kw)
        return call


@pytest.fixture
def env():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='ct_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(User(username='ct_worker', password=generate_password_hash('irrelevant123'), role='worker'))
        db.session.commit()
        clients = {}
        for n in ('admin', 'worker'):
            c = _Client(flask_app.test_client())
            c.post('/login', data={'username': f'ct_{n}', 'password': 'irrelevant123'})
            clients[n] = c
        yield clients
        db.session.remove()
        db.drop_all()


def _laser(name='Bodor PG3015'):
    m = Machine(name=name, is_laser=True)
    db.session.add(m)
    db.session.flush()
    hm = HallMachine(name=name, machine_id=m.id)
    db.session.add(hm)
    db.session.flush()
    return m, hm


def _grades():
    out = {n: MaterialGrade(name=n) for n in GRADES}
    db.session.add_all(out.values())
    db.session.flush()
    return out


def _tech(m, grade, mm, **kw):
    t = MachineCutTechnology(machine_id=m.id, grade_id=grade.id, thickness_mm=mm, gas='nitrogen', **kw)
    db.session.add(t)
    db.session.flush()
    return t


def test_default_false(env):
    m, _ = _laser()
    g_ = _grades()['Стомана']
    t = _tech(m, g_, 2)
    db.session.commit()
    assert t.needs_test is False


def test_tested_route(env):
    m, hm = _laser()
    g_ = _grades()['Стомана']
    t = _tech(m, g_, 2, needs_test=True)
    db.session.commit()
    url = f'/admin/hall/{hm.id}/cut-tech/{t.id}/tested'
    r = env['worker'].post(url)
    db.session.expire_all()
    assert MachineCutTechnology.query.get(t.id).needs_test is True
    assert r.status_code in (301, 302, 303, 403)
    r = env['admin'].post(url)
    assert r.status_code in (301, 302, 303)
    db.session.expire_all()
    assert MachineCutTechnology.query.get(t.id).needs_test is False


def test_dossier_badge_only_for_untested(env):
    m, hm = _laser()
    g_ = _grades()['Стомана']
    _tech(m, g_, 2, needs_test=True)
    db.session.commit()
    html = env['admin'].get(f'/admin/hall/{hm.id}/dossier').get_data(as_text=True)
    assert 'За тест - не е потвърдена' in html
    assert 'Работи ✓' in html
    MachineCutTechnology.query.update({'needs_test': False})
    db.session.commit()
    html = env['admin'].get(f'/admin/hall/{hm.id}/dossier').get_data(as_text=True)
    assert 'За тест - не е потвърдена' not in html
    assert 'Работи ✓' not in html


def test_apply_no_machine(env):
    _grades()
    assert apply() == (0, ['няма лазерна машина "PG3015"'])


def test_apply_adds_flagged_rows(env):
    m, _ = _laser()
    g = _grades()
    steel = g['Стомана']
    for mm in (2, 3, 6, 16, 20):
        _tech(m, steel, mm, p1_gas='nitrogen', pierce_steps=2)
    existing = _tech(m, steel, 1.5, cutting_speed_mm_per_min=111)
    brass = _tech(m, g['Месинг'], 2, p1_gas='nitrogen', p2_gas='nitrogen', pierce_steps=3)
    db.session.commit()
    before = MachineCutTechnology.query.count()

    added, skipped = apply()
    db.session.commit()
    assert added > 0
    assert MachineCutTechnology.query.count() == before + added
    new = MachineCutTechnology.query.filter_by(needs_test=True).all()
    assert len(new) == added
    # existing 1.5 mm steel untouched, not duplicated
    assert MachineCutTechnology.query.filter_by(grade_id=steel.id, thickness_mm=1.5).count() == 1
    assert MachineCutTechnology.query.get(existing.id).cutting_speed_mm_per_min == 111
    assert MachineCutTechnology.query.get(existing.id).needs_test is False
    # steel 2.5 from template steel 3 mm
    t25 = MachineCutTechnology.query.filter_by(grade_id=steel.id, thickness_mm=2.5).one()
    assert t25.needs_test and t25.pierce_steps == 2 and t25.p1_gas == 'nitrogen'
    # templates missing (stainless/aluminium/galvanized) -> skipped with a message
    assert any('няма шаблон' in s for s in skipped)
    assert MachineCutTechnology.query.filter_by(grade_id=g['Алуминий'].id).count() == 0
    # copper from brass template: pierce gas oxygen where template had one
    cu = MachineCutTechnology.query.filter_by(grade_id=g['Мед'].id, thickness_mm=1).one()
    assert cu.p1_gas == 'oxygen' and cu.p2_gas == 'oxygen' and not cu.p3_gas
    assert cu.needs_test and cu.pierce_steps == 3
    # second run adds nothing
    assert apply()[0] == 0


def test_apply_respects_thickness_range(env):
    m, _ = _laser()
    g = _grades()
    steel = g['Стомана']
    _tech(m, steel, 20, p1_gas='oxygen')
    db.session.add(MachineThicknessRange(machine_id=m.id, grade_id=steel.id, max_thickness_mm=20))
    db.session.commit()
    added, skipped = apply()
    assert not MachineCutTechnology.query.filter(MachineCutTechnology.grade_id == steel.id,
                                                 MachineCutTechnology.thickness_mm.in_((22, 25))).count()
    assert any('22' in s for s in skipped) and any('25' in s for s in skipped)
    assert any(p[0] == 'Стомана' and p[1] == 22 for p in PROPOSALS)


def test_library_fix_changes_only_untouched_proposals(env):
    """data_pg3015_tech_from_library: steel O2 14/18/22/25 mm proposals take the machine's own CypCut library values;
    an edited proposal is left alone; still "за тест"; a second run changes nothing."""
    from migration.data_pg3015_tech_from_library import apply as fix
    m, _ = _laser()
    steel = _grades()['Стомана']
    for mm in (16, 20):
        _tech(m, steel, mm, p1_gas='oxygen', pierce_steps=2)
    db.session.commit()
    apply()
    db.session.commit()
    t18 = MachineCutTechnology.query.filter_by(grade_id=steel.id, thickness_mm=18).one()
    t18.focus_mm = 12.8                                             # the owner edited this one
    db.session.commit()

    changed, notes = fix()
    db.session.commit()
    assert changed == 3 and any('18' in n for n in notes)
    by_mm = {t.thickness_mm: t for t in MachineCutTechnology.query.filter_by(grade_id=steel.id, gas='oxygen')}
    t25 = by_mm[25]
    assert (t25.nozzle, t25.cutting_speed_mm_per_min, t25.focus_mm, t25.gas_pressure_bar) == ('1.4sp/1.6sp', 300, 19, 1.2)
    assert t25.pierce_steps == 3 and t25.p1_stay_ms == 2000 and t25.p3_height_mm == 19 and t25.needs_test
    t14 = by_mm[14]
    assert (t14.nozzle, t14.cutting_speed_mm_per_min, t14.focus_mm, t14.p2_freq_hz) == ('1.2sp/1.4sp', 1400, 11, 200)
    assert (by_mm[22].focus_mm, by_mm[22].cutting_speed_mm_per_min) == (15, 500)
    assert by_mm[18].focus_mm == 12.8 and by_mm[18].cutting_speed_mm_per_min == 750      # edited: untouched
    assert fix()[0] == 0
