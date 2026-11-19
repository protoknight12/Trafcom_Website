"""
pytest: composed material names (owner's decision 2026-11-19) - compose_material_name() for each standard type,
missing parts and no grade; the materials dialog's new fields; add / update storing calibrated / calibration and
composing a blank name; the one-time data step migration/data_compose_material_names.py (lots renamed, remnant
suffix kept, ungraded lots left alone, delivery / client note snapshots rewritten, idempotent); optional node
cross-check of static/js/material_name.js against the Python.

    pytest testing/test_material_names.py -v
"""
import json
import os
import shutil
import subprocess

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, MaterialGrade, MaterialType, DeliveryNote, DeliveryNoteItem,
                 ClientDeliveryNote, ClientDeliveryNoteItem, limiter, seed_material_types, seed_material_grades,
                 compose_material_name, material_types_js)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        seed_material_grades()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _grade(name):
    return MaterialGrade.query.filter_by(name=name).one().id


def _lot(**kw):
    m = MaterialPrice(key=f"k{MaterialPrice.query.count() + 1}", display_name=kw.pop('display_name', 'стар'),
                      cost_per_m2=0, **kw)
    db.session.add(m)
    db.session.commit()
    return m


CASES = [   # (fields, expected)
    (dict(type='sheets', grade='Неръждаема стомана', brand='AISI 304', sheet_width_mm=1000, sheet_length_mm=2000,
          thickness_mm=2), 'Лист Неръждаема стомана AISI 304 1000×2000 2 мм'),
    (dict(type='rods', grade='Стомана', brand='S235JR', sheet_width_mm=20, sheet_length_mm=6000, calibrated=True,
          calibration='h9', notes='шлифован'), 'Прът Калиброван Стомана S235JR ⌀20×6000 h9 шлифован'),
    (dict(type='rods', grade='Стомана', brand='S235JR', sheet_width_mm=20, sheet_length_mm=6000, calibration='h9'),
     'Прът Стомана S235JR ⌀20×6000'),   # the class is ignored while not calibrated
    (dict(type='pipes', grade='Стомана', brand='S235JR', sheet_width_mm=40, sheet_length_mm=6000, thickness_mm=2),
     'Тръба Стомана S235JR ⌀40×6000 2 мм'),
    (dict(type='profiles', grade='Алуминий', sheet_width_mm=40, height_mm=20, sheet_length_mm=6000, thickness_mm=1.5),
     'Профил Алуминий 40×20×6000 1.5 мм'),
    (dict(type='sheets', brand='DC01', thickness_mm=1.25), 'Лист DC01 1.25 мм'),   # no grade, no size
    (dict(type='rods', grade='Стомана', sheet_length_mm=6000, thickness_mm=3), 'Прът Стомана 6000'),  # rods have no thickness
    (dict(type='other', grade='Мед', sheet_width_mm=10, notes='  '), 'Други Мед'),   # no singular -> label, no dims
]


def test_compose(admin_client):
    for fields, expected in CASES:
        fields = dict(fields)
        grade = fields.pop('grade', None)
        m = MaterialPrice(grade_id=_grade(grade) if grade else None, **fields)
        assert compose_material_name(m) == expected
    # override (a remnant piece of a sheet) and the custom singular of a type
    m = _lot(type='sheets', grade_id=_grade('Стомана'), sheet_width_mm=1500, sheet_length_mm=3000, thickness_mm=3)
    assert compose_material_name(m, sheet_width_mm=600, sheet_length_mm=400) == 'Лист Стомана 600×400 3 мм'
    MaterialType.query.filter_by(key='sheets').one().name_singular = None
    db.session.commit()
    from flask import g
    g.pop('_material_types', None)
    assert compose_material_name(m).startswith('Листове/Плочи Стомана')


def test_dialog_renders_new_fields(admin_client):
    html = admin_client.get('/admin/materials').get_data(as_text=True)
    for needle in ('name="calibrated"', 'name="calibration"', 'name="name_singular"', 'js/material_name.js',
                   'new_display_name_refresh', '(Прът)'):
        assert needle in html, needle


def _form(**extra):
    return dict({'type': 'rods', 'brand': 'S235JR', 'sheet_width_mm': '20', 'sheet_length_mm': '6000',
                 'grade_id': str(_grade('Стомана'))}, **extra)


def test_add_and_update_compose_blank_name(admin_client):
    c = admin_client
    c.post('/admin/add_material', data=_form(display_name='', calibrated='1', calibration='h9', notes='шлифован'))
    m = MaterialPrice.query.one()
    assert (m.display_name, m.calibrated, m.calibration) == ('Прът Калиброван Стомана S235JR ⌀20×6000 h9 шлифован', True, 'h9')

    c.post(f'/admin/update_material/{m.key}', data=_form(display_name='Моето име', calibration='h11'))   # unticked
    db.session.refresh(m)
    assert (m.display_name, m.calibrated, m.calibration) == ('Моето име', False, None)

    c.post(f'/admin/update_material/{m.key}', data=_form(display_name='  ', sheet_width_mm='25'))
    db.session.refresh(m)
    assert m.display_name == 'Прът Стомана S235JR ⌀25×6000'

    r = c.post('/api/quick-create-material', data=_form(display_name='', sheet_width_mm='30', calibrated='1',
                                                        calibration='h9'))
    assert r.get_json()['status'] == 'success'
    q = MaterialPrice.query.filter(MaterialPrice.id != m.id).one()
    assert (q.display_name, q.calibration) == ('Прът Калиброван Стомана S235JR ⌀30×6000 h9', 'h9')


def test_type_singular_saved(admin_client):
    t = MaterialType.query.filter_by(key='other').one()
    admin_client.post(f'/admin/material-types/{t.id}/edit', data={'label': 'Други', 'name_singular': 'Детайл',
                                                                   'price_unit': 'm2'})
    db.session.refresh(t)
    assert t.name_singular == 'Детайл'


def test_data_step(admin_client):
    from migration.data_compose_material_names import main
    steel = _grade('Стомана')
    sheet = _lot(display_name='Ламарина 2мм', type='sheets', grade_id=steel, brand='DC01', sheet_width_mm=1000,
                 sheet_length_mm=2000, thickness_mm=2)
    rest = _lot(display_name='Ламарина 2мм - остатък', type='sheets', grade_id=steel, brand='DC01',
                sheet_width_mm=600, sheet_length_mm=400, thickness_mm=2)
    rod = _lot(display_name='Прът 20', type='rods', grade_id=steel, sheet_width_mm=20, sheet_length_mm=6000,
               calibrated=True, calibration='h9')
    loose = _lot(display_name='Поликарбонат 5мм', type='sheets', thickness_mm=5)          # no Вид -> left alone
    note, cnote = DeliveryNote(), ClientDeliveryNote()
    db.session.add_all([note, cnote])
    db.session.flush()
    db.session.add_all([
        DeliveryNoteItem(delivery_note_id=note.id, target_type='material', target_id=sheet.id,
                         description_snapshot='Ламарина 2мм', quantity=1),
        DeliveryNoteItem(delivery_note_id=note.id, target_type='detail', target_id=sheet.id,   # a detail with the same id
                         description_snapshot='Детайл', quantity=1),
        DeliveryNoteItem(delivery_note_id=note.id, target_type='material', target_id=loose.id,
                         description_snapshot='Поликарбонат 5мм', quantity=1),
        ClientDeliveryNoteItem(client_delivery_note_id=cnote.id, target_type='material', target_id=rest.id,
                               description_snapshot='Ламарина 2мм - остатък', quantity=1),
    ])
    db.session.commit()

    renamed, snapshots, no_grade = main()
    db.session.expire_all()
    assert (renamed, snapshots, len(no_grade)) == (3, 2, 1)
    assert sheet.display_name == 'Лист Стомана DC01 1000×2000 2 мм'
    assert rest.display_name == 'Лист Стомана DC01 600×400 2 мм - остатък'
    assert rod.display_name == 'Прът Калиброван Стомана ⌀20×6000 h9'
    assert loose.display_name == 'Поликарбонат 5мм'
    snaps = {(i.target_type, i.target_id): i.description_snapshot for i in DeliveryNoteItem.query}
    assert snaps == {('material', sheet.id): sheet.display_name, ('detail', sheet.id): 'Детайл',
                     ('material', loose.id): 'Поликарбонат 5мм'}
    assert ClientDeliveryNoteItem.query.one().description_snapshot == rest.display_name

    assert main()[:2] == (0, 0)   # idempotent


def test_js_mirror_matches_python(admin_client):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    types = material_types_js()
    cases, expected = [], []
    for fields, want in CASES:
        f = dict(fields)
        cases.append({'type': f.get('type'), 'p': {
            'grade': f.get('grade'), 'brand': f.get('brand'), 'width': f.get('sheet_width_mm'),
            'height': f.get('height_mm'), 'length': f.get('sheet_length_mm'), 'thickness': f.get('thickness_mm'),
            'calibrated': f.get('calibrated'), 'calibration': f.get('calibration'), 'notes': f.get('notes')}})
        expected.append(want)
    src = open(os.path.join(ROOT, 'static', 'js', 'material_name.js'), encoding='utf-8').read()
    script = ('var window = {};\n' + src + '\nconst T = ' + json.dumps(types) + ', C = ' + json.dumps(cases) +
              ';\nconsole.log(JSON.stringify(C.map(c => window.composeMaterialName(T[c.type], c.p))));')
    out = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', check=True).stdout
    assert json.loads(out) == expected
