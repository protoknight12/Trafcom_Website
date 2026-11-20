"""
pytest: real (invoice) weight on a supplier delivery-note material line (owner's decision 2026-11-19) -
DeliveryNoteItem.weight_kg stored, the lot's weight_kg set per STOCK unit when asked (a sheet lot with a size:
per sheet; a linear lot: per metre), never for a sheet lot without a size; the computed basis stores nothing;
edit keeps / changes it; the print page and the list show "реално"; MaterialKg.bind's free mode (node).

Run with:
    pytest testing/test_delivery_note_real_weight.py -v
"""
import atexit
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, MaterialPrice, DeliveryNote, Client, limiter, seed_material_types)

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def admin_client():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(MaterialPrice(key='steel', display_name='Стомана 2мм', cost_per_m2=0.0,
                                     cutting_speed_mm_per_min=2000, pierce_rate_per_min=30, type='sheets',
                                     thickness_mm=2.0, sheet_width_mm=1000, sheet_length_mm=2000))
        db.session.add(MaterialPrice(key='nosize', display_name='Ламарина без размер', cost_per_m2=0.0,
                                     cutting_speed_mm_per_min=2000, pierce_rate_per_min=30, type='sheets',
                                     thickness_mm=2.0, weight_kg=99.0))
        db.session.add(MaterialPrice(key='rod', display_name='Прът ⌀20', cost_per_m2=0.0, type='rods',
                                     sheet_width_mm=20, sheet_length_mm=6000))
        db.session.add(Client(name='Клиент ООД'))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _sheet(qty, price=None, weight=None, set_lot=True, item_id=None):
    return {'type': 'material', 'name': 'Стомана 2мм', 'material_key': 'steel', 'qty': qty, 'unit_price': price,
            'width': 1000, 'height': 2000, 'thickness': 2.0, 'material_type': 'sheets', 'item_id': item_id,
            'weight_kg': weight, 'set_lot_weight': set_lot}


def _post(client, url, lines, **form):
    return client.post(url, data={'items_json': json.dumps(lines), **form})


def _lot(key):
    db.session.expire_all()
    return MaterialPrice.query.filter_by(key=key).one()


def test_real_weight_stored_and_lot_weight_per_sheet(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(5, price=40, weight=160)])
    item = DeliveryNote.query.one().items[0]
    assert item.weight_kg == 160
    assert _lot('steel').weight_kg == 32   # 160 kg / 5 sheets


def test_checkbox_off_leaves_lot_alone(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(5, weight=160, set_lot=False)])
    assert DeliveryNote.query.one().items[0].weight_kg == 160
    assert _lot('steel').weight_kg is None


def test_computed_basis_stores_nothing(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(5, price=40)])
    assert DeliveryNote.query.one().items[0].weight_kg is None
    assert _lot('steel').weight_kg is None


def test_linear_lot_weight_per_metre(admin_client):
    line = {'type': 'material', 'name': 'Прът ⌀20', 'material_key': 'rod', 'qty': 12, 'unit_price': None,
            'width': 20, 'height': 6000, 'material_type': 'rods', 'weight_kg': 30, 'set_lot_weight': True}
    _post(admin_client, '/admin/delivery-notes/create', [line])
    assert _lot('rod').weight_kg == 2.5   # 30 kg / 12 m


def test_sheet_lot_without_size_not_updated(admin_client):
    line = {'type': 'material', 'name': 'Ламарина без размер', 'material_key': 'nosize', 'qty': 4,
            'thickness': 2.0, 'material_type': 'sheets', 'weight_kg': 70, 'set_lot_weight': True}
    _post(admin_client, '/admin/delivery-notes/create', [line])
    assert DeliveryNote.query.one().items[0].weight_kg == 70
    assert _lot('nosize').weight_kg == 99.0


def test_new_price_lot_gets_the_weight(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(2, price=40)])
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(4, price=60, weight=140)])
    db.session.expire_all()
    lots = {m.price_per_unit: m.weight_kg for m in MaterialPrice.query.filter_by(display_name='Стомана 2мм')}
    assert lots == {40: None, 60: 35}, lots


def test_client_receipt_records_real_kg_without_prices(admin_client):
    client_id = Client.query.one().id
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(2, price=40, weight=70)], client_id=client_id)
    item = DeliveryNote.query.one().items[0]
    assert item.weight_kg == 70 and item.unit_price is None
    db.session.expire_all()
    client_lot = MaterialPrice.query.filter_by(owner_client_id=client_id).one()
    assert client_lot.weight_kg == 35 and not client_lot.cost_per_m2


def test_edit_keeps_and_changes_real_weight(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(5, price=40, weight=160)])
    note = DeliveryNote.query.one()
    item_id = note.items[0].id
    # kept as-is (the dialog sends the stored weight back, checkbox off)
    _post(admin_client, f'/admin/delivery-notes/{note.id}/edit', [_sheet(5, 40, 160, False, item_id)])
    db.session.expire_all()
    note = DeliveryNote.query.one()
    assert note.items[0].weight_kg == 160 and _lot('steel').weight_kg == 32
    # corrected and written to the lot again
    _post(admin_client, f'/admin/delivery-notes/{note.id}/edit', [_sheet(5, 40, 150, True, note.items[0].id)])
    db.session.expire_all()
    assert DeliveryNote.query.one().items[0].weight_kg == 150 and _lot('steel').weight_kg == 30
    # back to the computed basis
    _post(admin_client, f'/admin/delivery-notes/{note.id}/edit', [_sheet(5, 40, None, False, DeliveryNote.query.one().items[0].id)])
    db.session.expire_all()
    assert DeliveryNote.query.one().items[0].weight_kg is None


def test_print_list_and_dialog_show_real_weight(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(5, price=40, weight=160.456)])
    note = DeliveryNote.query.one()
    html = admin_client.get(f'/admin/delivery-notes/{note.id}/print').get_data(as_text=True)
    assert 'реално 160.46 кг' in html
    page = admin_client.get('/admin/delivery-notes').get_data(as_text=True)
    assert 'реално 160.456 кг' in page                       # history
    assert '"weight_kg": 160.456' in page                    # NOTE_ROWS for the edit dialog
    for needle in ('name="dn_weight_basis" value="real"', 'id="dn_set_lot_weight"', 'id="dn_kg_hint"'):
        assert needle in page


def test_print_computed_weight_unchanged(admin_client):
    _post(admin_client, '/admin/delivery-notes/create', [_sheet(5, price=40)])
    html = admin_client.get(f'/admin/delivery-notes/{DeliveryNote.query.one().id}/print').get_data(as_text=True)
    assert 'реално' not in html and '(≈ 157 кг)' in html   # 5 x 1 x 2 m² x 2 mm x 7.85


def test_bind_free_mode_node(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node не е инсталиран - проверката на JS е пропусната')
    script = tmp_path / 'run.js'
    script.write_text(r"""
const MK = require(process.argv[2]);
function el(v) { const h = {}; return { value: v, style: {}, form: null, disabled: false,
    addEventListener(t, f) { (h[t] = h[t] || []).push(f); },
    dispatchEvent(e) { (h[e.type] || []).forEach(f => f(e)); }, fire(t) { (h[t] || []).forEach(f => f({type: t})); } }; }
global.Event = function (t) { this.type = t; };
const qty = el('5'), kg = el('');
let free = false;
const b = MK.bind(qty, kg, () => 31.4, null, [], () => free);
const out = {computed: kg.value};
free = true; b.refresh();
kg.value = '160'; kg.fire('input');
out.qtyAfterKg = qty.value;
qty.value = '6'; qty.fire('input');
out.kgAfterQty = kg.value;
free = false; b.refresh();
out.back = kg.value;
console.log(JSON.stringify(out));
""", encoding='utf-8')
    run = subprocess.run([node, str(script), str(ROOT / 'static' / 'js' / 'material_weight.js')],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out == {'computed': 157, 'qtyAfterKg': '5', 'kgAfterQty': '160', 'back': 188.4}, out


def test_linear_line_shows_bars_and_prices_per_bar(admin_client):
    """A rod line: quantity in metres, unit_price per bar of the line's length - print/list/history show
    "2 бр. = 12 м" and the value is bars x price (40.00), not metres x price (240.00)."""
    line = {'type': 'material', 'name': 'Прът ⌀20', 'material_key': 'rod', 'qty': 12, 'unit_price': 20,
            'width': 20, 'height': 6000, 'material_type': 'rods'}
    _post(admin_client, '/admin/delivery-notes/create', [line])
    note = DeliveryNote.query.one()
    html = admin_client.get(f'/admin/delivery-notes/{note.id}/print').get_data(as_text=True)
    assert '2 бр. = 12 м' in html
    assert '40.00' in html and '240.00' not in html
    listing = admin_client.get('/admin/delivery-notes').get_data(as_text=True)
    assert '2 бр. = 12 м' in listing and 'Общо: 40.00 €' in listing
    lot = _lot('rod')
    assert '2 бр. = 12 м' in admin_client.get(f'/admin/materials/{lot.id}/history').get_data(as_text=True)
