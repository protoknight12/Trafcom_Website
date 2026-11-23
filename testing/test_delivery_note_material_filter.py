"""
pytest: the "Материал" filter on the delivery-note histories (admin_delivery_notes.html and
admin_client_delivery_notes.html) - each row lists its material ids in data-materials, the filter select
(#filterMaterial, data-picker="material", remembered by remember_filters.js) offers only the materials some note
names, and materialFilterTest() in static/js/cascade_filters.js keeps just the rows with that material.

Run with:
    pytest testing/test_delivery_note_material_filter.py -v
"""
import os
import re
import shutil
import subprocess

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, Supplier, MaterialPrice, DeliveryNote, DeliveryNoteItem,
                 ClientDeliveryNote, ClientDeliveryNoteItem, limiter, seed_material_types)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        mats = [MaterialPrice(key=k, display_name=n, cost_per_m2=0.0, type='sheets', thickness_mm=2.0)
                for k, n in (('m_a', 'Лист А'), ('m_b', 'Лист Б'), ('m_unused', 'Лист без разписка'))]
        sup, cli = Supplier(name='Доставчик 1'), Client(name='Клиент 1')
        db.session.add_all(mats + [sup, cli])
        db.session.flush()
        a, b = mats[0], mats[1]
        n1 = DeliveryNote(supplier_id=sup.id, note_number='F-1')
        n2 = DeliveryNote(supplier_id=sup.id, note_number='F-2')
        db.session.add_all([n1, n2])
        c1 = ClientDeliveryNote(client_id=cli.id, kind='issue', note_number='C-1')
        db.session.add(c1)
        db.session.flush()
        db.session.add_all([
            DeliveryNoteItem(delivery_note_id=n1.id, target_type='material', target_id=a.id,
                             description_snapshot='Лист А', quantity=1),
            DeliveryNoteItem(delivery_note_id=n1.id, target_type='material', target_id=b.id,
                             description_snapshot='Лист Б', quantity=2),
            DeliveryNoteItem(delivery_note_id=n2.id, target_type='material', target_id=b.id,
                             description_snapshot='Лист Б', quantity=3),
            ClientDeliveryNoteItem(client_delivery_note_id=c1.id, target_type='material', target_id=a.id,
                                   description_snapshot='Лист А', quantity=1),
        ])
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        c.ids = {'a': a.id, 'b': b.id}
        yield c
        db.session.remove()
        db.drop_all()


def _filter_select(html):
    m = re.search(r'<select id="filterMaterial" data-picker="material".*?</select>', html, re.S)
    assert m, 'no material filter select'
    return m.group(0)


def test_supplier_history_rows_and_options(admin_client):
    html = admin_client.get('/admin/delivery-notes').get_data(as_text=True)
    ids = admin_client.ids
    rows = re.findall(r'data-materials="([^"]*)"', html)
    assert sorted(rows) == sorted([f"{ids['a']},{ids['b']}", str(ids['b'])])
    sel = _filter_select(html)
    assert 'value="m_a"' in sel and 'value="m_b"' in sel and 'm_unused' not in sel
    assert f'data-id="{ids["a"]}"' in sel
    assert 'id="filterParty"' in html and 'id="dnHistorySearchInput"' in html


def test_client_history_rows_and_options(admin_client):
    html = admin_client.get('/admin/client-delivery-notes').get_data(as_text=True)
    assert re.findall(r'data-materials="([^"]*)"', html) == [str(admin_client.ids['a'])]
    sel = _filter_select(html)
    assert 'value="m_a"' in sel and 'value="m_b"' not in sel


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_material_filter_test_js():
    js = open(os.path.join(ROOT, 'static', 'js', 'cascade_filters.js'), encoding='utf-8').read()
    script = js + r"""
    const sel = {options: [{value: '', dataset: {}}, {value: 'm_a', dataset: {id: '1'}}, {value: 'm_b', dataset: {id: '12'}}]};
    const t = materialFilterTest(sel);
    const row = m => ({dataset: {materials: m}});
    const out = [t(row('1,5'), 'm_a'), t(row('11,12'), 'm_a'), t(row('11,12'), 'm_b'), t(row(''), 'm_a')];
    console.log(JSON.stringify(out));
    """
    res = subprocess.run(['node', '-e', 'const window = globalThis;' + script], capture_output=True, text=True,
                         timeout=30)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == '[true,false,true,false]'
