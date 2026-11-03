"""
pytest: client stock (материали на съхранение) - materials and semi-finished
parts a client leaves with us stay the client's property: ownership on
MaterialPrice (owner_client_id), receipt / return / consume documents, and
"client lots are never our material" visibility.

Run with:
    pytest testing/test_client_stock.py -v
"""
import atexit
import json
import os
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

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Supplier, DeliveryNote, DeliveryNoteItem,
                 limiter, seed_material_types, _find_or_create_delivery_target, format_material_option,
                 own_materials_query)


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
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def test_matching_separates_owner(admin_client):
    with flask_app.app_context():
        c = Client(name='Клиент А'); db.session.add(c); db.session.commit()
        ours = MaterialPrice.query.filter_by(key='steel').one()
        lot = _find_or_create_delivery_target('material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, 'steel',
                                              material_type='sheets', owner_client_id=c.id)
        assert lot.id != ours.id and lot.owner_client_id == c.id and lot.cost_per_m2 == 0
        assert lot.cutting_speed_mm_per_min == ours.cutting_speed_mm_per_min   # copied from the picked row
        again = _find_or_create_delivery_target('material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, 'steel',
                                                material_type='sheets', owner_client_id=c.id)
        assert again.id == lot.id                                              # second receipt reuses the client lot
        ours_again = _find_or_create_delivery_target('material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, 'steel',
                                                     material_type='sheets')
        assert ours_again.id == ours.id


def test_option_text_shows_owner(admin_client):
    with flask_app.app_context():
        c = Client(name='Клиент А'); db.session.add(c); db.session.flush()
        m = MaterialPrice(key='x', display_name='Мед', cost_per_m2=0, type='sheets', owner_client_id=c.id)
        db.session.add(m); db.session.flush()
        assert format_material_option(m).endswith(' [клиент: Клиент А]')
        assert own_materials_query().filter_by(key='x').first() is None


def _line(qty, price=None, item_id=None):
    return {'type': 'material', 'name': 'Стомана 2мм', 'material_key': 'steel', 'qty': qty, 'unit_price': price,
            'width': 1000, 'height': 2000, 'thickness': 2.0, 'material_type': 'sheets', 'item_id': item_id}


def _client(name='Клиент А'):
    c = Client(name=name)
    db.session.add(c)
    db.session.commit()
    return c


def _receipt(client, c_id, lines, note_id=None, **extra):
    url = f'/admin/delivery-notes/{note_id}/edit' if note_id else '/admin/delivery-notes/create'
    return client.post(url, data={'client_id': c_id, 'items_json': json.dumps(lines), **extra}, follow_redirects=True)


def test_client_receipt_creates_owner_lot(admin_client):
    c = _client()
    # line picked as 'steel' (our lot) with a unit price -> client lot, price ignored, our lot untouched
    r = _receipt(admin_client, c.id, [_line(3, 55.0)])
    assert r.status_code == 200
    lot = MaterialPrice.query.filter_by(owner_client_id=c.id).one()
    assert lot.stock_quantity == 3 and lot.cost_per_m2 == 0 and lot.price_per_unit is None
    assert MaterialPrice.query.filter_by(key='steel').one().stock_quantity in (None, 0)
    item = DeliveryNoteItem.query.one()
    assert item.unit_price is None and item.target_id == lot.id
    assert DeliveryNote.query.one().client_id == c.id


def test_client_receipt_from_our_lot_pick(admin_client):   # Review Focus 1
    c = _client()
    _receipt(admin_client, c.id, [_line(2)])
    lot = MaterialPrice.query.filter_by(owner_client_id=c.id).one()
    assert lot.cutting_speed_mm_per_min == 2000 and lot.pierce_rate_per_min == 30
    # a second receipt reuses the client lot instead of creating another
    _receipt(admin_client, c.id, [_line(1)])
    db.session.expire_all()
    assert MaterialPrice.query.filter_by(owner_client_id=c.id).one().stock_quantity == 3


def test_client_receipt_both_parties_refused(admin_client):
    c = _client()
    s = Supplier(name='Доставчик'); db.session.add(s); db.session.commit()
    r = admin_client.post('/admin/delivery-notes/create', data={
        'client_id': c.id, 'supplier_id': s.id, 'items_json': json.dumps([_line(1)])}, follow_redirects=True)
    assert 'не и двете' in r.get_data(as_text=True) and DeliveryNote.query.count() == 0


def test_client_receipt_detail_line(admin_client):
    c = _client()
    line = {'type': 'detail', 'name': 'Фланец', 'material_key': 'steel', 'qty': 10, 'unit_price': None}
    _receipt(admin_client, c.id, [line])
    d = Detail.query.filter_by(name='Фланец').one()
    assert d.owner_client_id == c.id and d.stock_quantity == 10 and d.calculated_price == 0


def test_client_receipt_rejected_line_reported(admin_client):
    c = _client()
    bad = {'type': 'material', 'name': 'Нещо ново', 'material_key': None, 'qty': 1, 'material_type': 'sheets'}
    r = _receipt(admin_client, c.id, [bad, _line(2)])
    html = r.get_data(as_text=True)
    assert 'Нещо ново' in html and 'не беше записан (липсват данни' in html   # Jinja escapes the quotes
    assert MaterialPrice.query.filter_by(owner_client_id=c.id).one().stock_quantity == 2   # the rest is saved


def test_client_receipt_edit_keeps_party(admin_client):
    c = _client()
    s = Supplier(name='Доставчик'); db.session.add(s); db.session.commit()
    _receipt(admin_client, c.id, [_line(3)])
    note = DeliveryNote.query.one()
    admin_client.post(f'/admin/delivery-notes/{note.id}/edit', data={
        'supplier_id': s.id, 'client_id': '', 'note_number': 'N7',
        'items_json': json.dumps([_line(5, None, note.items[0].id)])})
    db.session.expire_all()
    note = DeliveryNote.query.one()
    assert note.client_id == c.id and note.supplier_id is None and note.note_number == 'N7'
    assert MaterialPrice.query.filter_by(owner_client_id=c.id).one().stock_quantity == 5


def test_supplier_receipt_unchanged(admin_client):
    s = Supplier(name='Доставчик'); db.session.add(s); db.session.commit()
    admin_client.post('/admin/delivery-notes/create', data={'supplier_id': s.id, 'items_json': json.dumps([_line(4, 40)])})
    note = DeliveryNote.query.one()
    assert note.supplier_id == s.id and note.client_id is None
    assert DeliveryNoteItem.query.one().unit_price == 40


def test_client_receipt_print(admin_client):
    c = _client()
    _receipt(admin_client, c.id, [_line(3)])
    note = DeliveryNote.query.one()
    html = admin_client.get(f'/admin/delivery-notes/{note.id}/print').get_data(as_text=True)
    assert 'ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ' in html and 'Клиент А' in html and 'Ед. цена' not in html
    # the list page renders the party label
    assert 'Клиент А (съхранение)' in admin_client.get('/admin/delivery-notes').get_data(as_text=True)


# ---------- Task 3: consume / return with remnant, negative-stock guard ----------

from app import ClientDeliveryNote, ClientDeliveryNoteItem


def _out(client, c_id, kind, lines, note_id=None, **extra):
    url = f'/admin/client-delivery-notes/{note_id}/edit' if note_id else '/admin/client-delivery-notes/create'
    return client.post(url, data={'client_id': c_id or '', 'kind': kind, 'items_json': json.dumps(lines), **extra},
                       follow_redirects=True)


def _lot(owner_id, stock, key, type_='sheets', name='Стомана 2мм', w=1000, h=2000):
    m = MaterialPrice(key=key, display_name=name, cost_per_m2=0, cutting_speed_mm_per_min=2000, pierce_rate_per_min=30,
                      type=type_, thickness_mm=2.0, sheet_width_mm=w, sheet_length_mm=h, owner_client_id=owner_id,
                      stock_quantity=stock)
    db.session.add(m)
    db.session.commit()
    return m


def _setup():
    """Client A with a 1000x2000x2 lot (5), client B with its own lot (5), our 'steel' (4)."""
    a, b = _client('Клиент А'), _client('Клиент Б')
    la, lb = _lot(a.id, 5, 'lot_a'), _lot(b.id, 5, 'lot_b')
    MaterialPrice.query.filter_by(key='steel').one().stock_quantity = 4
    db.session.commit()
    return a.id, b.id, la.id, lb.id


def _stock(mid):
    db.session.expire_all()
    return db.session.get(MaterialPrice, mid).stock_quantity


def _mline(mid, qty, **remnant):
    return {'type': 'material', 'target_id': mid, 'qty': qty, 'unit_price': None, 'notes': None, **remnant}


def test_consume_foreign_lot_refused(admin_client):
    a, b, la, lb = _setup()
    html = _out(admin_client, a, 'consume', [_mline(lb, 1)]).get_data(as_text=True)
    assert 'не е партида на клиента' in html
    assert _stock(lb) == 5 and _stock(la) == 5 and ClientDeliveryNote.query.count() == 0
    # our own material is not the client's lot either
    steel = MaterialPrice.query.filter_by(key='steel').one().id
    html = _out(admin_client, a, 'consume', [_mline(steel, 1)]).get_data(as_text=True)
    assert 'не е партида на клиента' in html and ClientDeliveryNote.query.count() == 0


def test_consume_requires_client(admin_client):
    a, b, la, lb = _setup()
    html = _out(admin_client, None, 'consume', [_mline(la, 1)]).get_data(as_text=True)
    assert 'Изписването изисква клиент.' in html and _stock(la) == 5 and ClientDeliveryNote.query.count() == 0


def test_consume_decrements_and_sheet_remnant_creates_lot(admin_client):
    a, b, la, lb = _setup()
    r = _out(admin_client, a, 'consume', [_mline(la, 2, remnant_qty=1, remnant_width=600, remnant_height=900)])
    assert r.status_code == 200
    assert _stock(la) == 3
    rem = MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').one()
    assert rem.owner_client_id == a and rem.stock_quantity == 1 and rem.cost_per_m2 == 0
    assert (rem.sheet_width_mm, rem.sheet_length_mm, rem.thickness_mm) == (600, 900, 2.0)
    item = ClientDeliveryNoteItem.query.one()
    assert item.remnant_material_id == rem.id and item.remnant_qty == 1
    assert ClientDeliveryNote.query.one().kind == 'consume'
    # a second remnant with the same dims lands on the same lot
    _out(admin_client, a, 'consume', [_mline(la, 1, remnant_qty=1, remnant_width=600, remnant_height=900)])
    assert MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').count() == 1
    assert _stock(rem.id) == 2 and _stock(la) == 2


def test_consume_remnant_linear_same_lot(admin_client):   # Review Focus 2
    a = _client('Клиент А')
    rods = _lot(a.id, 10, 'rods_a', type_='rods', name='Прът ф20', w=20, h=6000)
    rods_id, count = rods.id, MaterialPrice.query.count()
    _out(admin_client, a.id, 'consume', [_mline(rods_id, 3, remnant_qty=1.5)])
    assert _stock(rods_id) == 8.5
    assert ClientDeliveryNoteItem.query.one().remnant_material_id == rods_id
    assert MaterialPrice.query.count() == count


def test_consume_sheet_remnant_without_dims_refused(admin_client):
    a, b, la, lb = _setup()
    html = _out(admin_client, a, 'consume', [_mline(la, 2, remnant_qty=1, remnant_width=600)]).get_data(as_text=True)
    assert 'трябва да има ширина и дължина' in html
    assert _stock(la) == 5 and ClientDeliveryNote.query.count() == 0


def test_consume_over_stock_refused(admin_client):
    a, b, la, lb = _setup()
    html = _out(admin_client, a, 'consume', [_mline(la, 6)]).get_data(as_text=True)
    assert 'отрицателна' in html and _stock(la) == 5 and ClientDeliveryNote.query.count() == 0


def test_edit_and_delete_consume_reverse_remnant(admin_client):
    a, b, la, lb = _setup()
    _out(admin_client, a, 'consume', [_mline(la, 2, remnant_qty=1, remnant_width=600, remnant_height=900)])
    note_id = ClientDeliveryNote.query.one().id
    rem_id = MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').one().id
    _out(admin_client, a, 'consume', [_mline(la, 1)], note_id=note_id)
    assert _stock(la) == 4 and _stock(rem_id) == 0
    admin_client.post(f'/admin/client-delivery-notes/{note_id}/delete')
    assert _stock(la) == 5 and _stock(rem_id) == 0 and ClientDeliveryNote.query.count() == 0


def test_delete_consume_after_remnant_used_refused(admin_client):
    a, b, la, lb = _setup()
    _out(admin_client, a, 'consume', [_mline(la, 2, remnant_qty=1, remnant_width=600, remnant_height=900)])
    first = ClientDeliveryNote.query.one().id
    rem_id = MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').one().id
    _out(admin_client, a, 'consume', [_mline(rem_id, 1)])   # the remnant itself gets used
    html = admin_client.post(f'/admin/client-delivery-notes/{first}/delete', follow_redirects=True).get_data(as_text=True)
    assert 'отрицателна' in html and 'остатък' in html
    assert ClientDeliveryNote.query.count() == 2 and _stock(la) == 3 and _stock(rem_id) == 0


def test_delete_receipt_after_consume_refused(admin_client):   # Review Focus 3
    c = _client()
    _receipt(admin_client, c.id, [_line(3)])
    lot = MaterialPrice.query.filter_by(owner_client_id=c.id).one()
    lot_id, note_id = lot.id, DeliveryNote.query.one().id
    _out(admin_client, c.id, 'consume', [_mline(lot_id, 2)])
    assert _stock(lot_id) == 1
    html = admin_client.post(f'/admin/delivery-notes/{note_id}/delete', follow_redirects=True).get_data(as_text=True)
    assert 'отрицателна' in html and 'Стомана 2мм' in html
    assert DeliveryNote.query.count() == 1 and _stock(lot_id) == 1
    # editing the receipt down below what was consumed is refused the same way
    item_id = DeliveryNote.query.one().items[0].id
    html = _receipt(admin_client, c.id, [_line(1, None, item_id)], note_id=note_id).get_data(as_text=True)
    assert 'отрицателна' in html and _stock(lot_id) == 1
    assert DeliveryNoteItem.query.one().quantity == 3


def test_return_other_clients_lot_refused_our_stock_ok(admin_client):
    a, b, la, lb = _setup()
    steel = MaterialPrice.query.filter_by(key='steel').one().id
    html = _out(admin_client, a, 'return', [_mline(lb, 1)]).get_data(as_text=True)
    assert 'принадлежи на друг клиент' in html and _stock(lb) == 5 and ClientDeliveryNote.query.count() == 0
    # our stock and the client's own lot can go back to the client; remnant keys are ignored on a return
    _out(admin_client, a, 'return', [_mline(steel, 1), _mline(la, 2, remnant_qty=1, remnant_width=600, remnant_height=900)])
    assert _stock(steel) == 3 and _stock(la) == 3
    assert all(it.remnant_qty is None and it.remnant_material_id is None for it in ClientDeliveryNoteItem.query.all())
    # our own lot may still go below zero (non-blocking, as before)
    _out(admin_client, a, 'return', [_mline(steel, 10)])
    assert _stock(steel) == -7


def test_edit_ignores_kind_and_client(admin_client):
    a, b, la, lb = _setup()
    _out(admin_client, a, 'consume', [_mline(la, 1)])
    note_id = ClientDeliveryNote.query.one().id
    _out(admin_client, b, 'return', [_mline(la, 2)], note_id=note_id)
    db.session.expire_all()
    note = db.session.get(ClientDeliveryNote, note_id)
    assert note.kind == 'consume' and note.client_id == a and _stock(la) == 3


def test_unknown_kind_falls_back_to_return(admin_client):
    a, b, la, lb = _setup()
    _out(admin_client, a, 'bogus', [_mline(la, 1)])
    assert ClientDeliveryNote.query.one().kind == 'return'


def test_client_note_print_titles(admin_client):
    a, b, la, lb = _setup()
    _out(admin_client, a, 'consume', [_mline(la, 2, remnant_qty=1, remnant_width=600, remnant_height=900), _mline(la, 1)])
    _out(admin_client, a, 'return', [_mline(la, 1)])
    consume, ret = ClientDeliveryNote.query.order_by(ClientDeliveryNote.id).all()
    html = admin_client.get(f'/admin/client-delivery-notes/{consume.id}/print').get_data(as_text=True)
    assert 'ПРОТОКОЛ ЗА ИЗПИСВАНЕ' in html and '600' in html and 'Ед. цена' not in html
    html = admin_client.get(f'/admin/client-delivery-notes/{ret.id}/print').get_data(as_text=True)
    assert 'ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ' in html and 'връщане' in html and 'Клиент А' in html
    page = admin_client.get('/admin/client-delivery-notes')
    assert page.status_code == 200 and 'Изписване за производство' in page.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Task 4: a client's lot is never "our material"
# ---------------------------------------------------------------------------

def _client_lot_named(name='Клиентска мед', key='clientlot'):
    c = _client()
    _lot(c.id, 5, key, name=name)
    return c


def _regular_user_client():
    db.session.add(User(username='qa_regular', password=generate_password_hash('irrelevant123'), role='regular_user'))
    db.session.commit()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_regular', 'password': 'irrelevant123'})
    return c


def test_client_lot_hidden_from_pickers(admin_client):
    _client_lot_named()
    for who in (_regular_user_client(), admin_client):
        for url in ('/dashboard', '/upload', '/orders/new', '/generator', '/flashing'):
            r = who.get(url)
            assert r.status_code == 200, url
            assert 'Клиентска мед' not in r.get_data(as_text=True), url
    # the stock page (Task 5 adds owner filters) still lists every lot
    assert 'Клиентска мед' in admin_client.get('/storage/materials').get_data(as_text=True)


def _dxf_file(tmp_path):
    import ezdxf
    doc = ezdxf.new('R2000')
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
    p = tmp_path / 'sq.dxf'
    doc.saveas(str(p))
    return p


def test_upload_client_lot_refused(admin_client, tmp_path):   # Review Focus 4
    from app import DxfFile
    _client_lot_named()
    user = _regular_user_client()
    with open(_dxf_file(tmp_path), 'rb') as f:
        r = user.post('/upload', data={'material': 'clientlot', 'file': (f, 'sq.dxf')},
                      content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    assert 'Невалиден избор на материал' in r.get_data(as_text=True)
    assert DxfFile.query.count() == 0


def test_chatbot_tools_hide_client_lots(admin_client):
    from flask_login import login_user
    from app import search_catalog, list_materials, get_material_details, most_expensive_items
    _client_lot_named()
    _regular_user_client()
    for username in ('qa_regular', 'qa_admin'):
        with flask_app.test_request_context():
            login_user(User.query.filter_by(username=username).first())
            assert 'Клиентска мед' not in list_materials.func()
            assert 'Клиентска мед' not in search_catalog.func('Клиентска')
            assert 'Клиентска мед' not in get_material_details.func('Клиентска')
            assert 'Клиентска мед' not in most_expensive_items.func()


def test_production_orders_skip_client_lots(admin_client):
    from app import ProductionOrder
    c = _client()
    lot = _lot(c.id, 5, 'clientlot')              # same name/dims as our 'steel' -> would be offered as a price lot
    det = Detail(name='Скоба', material_key='steel', width=100, height=50, total_length=300, pierce_count=1,
                 calculated_price=1.0)
    db.session.add(det)
    db.session.commit()
    page = admin_client.get('/admin/production-orders').get_data(as_text=True)
    assert '[клиент:' not in page
    r = admin_client.post('/admin/production-orders/create',
                          data={'detail_id': det.id, 'material_id': lot.id, 'quantity': 1}, follow_redirects=True)
    assert 'клиентска партида' in r.get_data(as_text=True).lower()
    assert ProductionOrder.query.count() == 0


def test_delete_client_with_lot_refused(admin_client):
    c = _client_lot_named()
    r = admin_client.post(f'/admin/clients/{c.id}/delete', follow_redirects=True)
    assert 'партиди' in r.get_data(as_text=True)
    assert db.session.get(Client, c.id) is not None


# ---------- Task 5: owner in stock lists and lot history ----------

def test_storage_owner_filter(admin_client):
    a, b, la, lb = _setup()
    own = MaterialPrice.query.filter_by(key='steel').one()
    own_name = own.display_name
    # same name/dims/type as our 'steel' - must NOT merge into one group with the client lots
    html = admin_client.get('/storage/materials').get_data(as_text=True)
    assert 'Собственик' in html and '[клиент: Клиент А]' in html and '[клиент: Клиент Б]' in html
    assert 'партиди' not in html                      # 3 owners -> 3 plain rows, no merged group
    assert html.count(own_name) >= 3
    html = admin_client.get('/storage/materials?owner=own').get_data(as_text=True)
    assert '[клиент:' not in html and own_name in html
    html = admin_client.get(f'/storage/materials?owner=client:{a}').get_data(as_text=True)
    assert '[клиент: Клиент А]' in html and '[клиент: Клиент Б]' not in html
    assert f'/admin/materials/{la}/history' in html and f'/admin/materials/{own.id}/history' not in html
    # garbage falls back to everything
    html = admin_client.get('/storage/materials?owner=client:x').get_data(as_text=True)
    assert '[клиент: Клиент А]' in html and '[клиент: Клиент Б]' in html


def test_admin_materials_owner_column(admin_client):
    a, b, la, lb = _setup()
    html = admin_client.get('/admin/materials').get_data(as_text=True)
    assert 'id="filterOwner"' in html and 'data-owner="own"' in html and f'data-owner="{a}"' in html


def test_history_kinds(admin_client):
    c = _client()
    _receipt(admin_client, c.id, [_line(5)])
    lot_id = MaterialPrice.query.filter_by(owner_client_id=c.id).one().id
    _out(admin_client, c.id, 'consume', [_mline(lot_id, 2, remnant_qty=1, remnant_width=600, remnant_height=900)])
    rem_id = MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').one().id
    html = admin_client.get(f'/admin/materials/{lot_id}/history').get_data(as_text=True)
    assert 'Приемане на съхранение' in html and 'Изписано за производство' in html
    assert 'Клиент А' in html
    html = admin_client.get(f'/admin/materials/{rem_id}/history').get_data(as_text=True)
    assert 'Остатък' in html and 'остатък от' in html
    # a return is labelled as such
    _out(admin_client, c.id, 'return', [_mline(lot_id, 1)])
    html = admin_client.get(f'/admin/materials/{lot_id}/history').get_data(as_text=True)
    assert 'Връщане на клиент' in html


# ---------- review fixes ----------

def test_return_note_ships_private_detail_unreconciled(admin_client):   # fix 1
    a, b, la, lb = _setup()
    det = Detail(name='Скоба', material_key='steel', width=100, height=50, total_length=300, pierce_count=1,
                 calculated_price=1.0, owner_client_id=a, stock_quantity=0)
    db.session.add(det)
    db.session.commit()
    det_id = det.id
    _out(admin_client, a, 'return', [{'type': 'detail', 'target_id': det_id, 'qty': 3, 'unit_price': None, 'notes': None}])
    db.session.expire_all()
    assert ClientDeliveryNote.query.count() == 1 and db.session.get(Detail, det_id).stock_quantity == -3


def test_linear_remnant_not_more_than_consumed(admin_client):   # fix 2
    a = _client('Клиент А')
    rods_id = _lot(a.id, 5, 'rods_a', type_='rods', name='Прът ф20', w=20, h=6000).id
    html = _out(admin_client, a.id, 'consume', [_mline(rods_id, 1, remnant_qty=50)]).get_data(as_text=True)
    assert 'не може да е повече от изразходваното' in html
    assert _stock(rods_id) == 5 and ClientDeliveryNote.query.count() == 0


def test_return_without_client_cannot_issue_client_lot(admin_client):   # fix 3
    a, b, la, lb = _setup()
    html = _out(admin_client, None, 'return', [_mline(la, 1)]).get_data(as_text=True)
    assert 'принадлежи на друг клиент' in html and _stock(la) == 5 and ClientDeliveryNote.query.count() == 0


def test_receipt_detail_on_other_clients_lot_rejected(admin_client):   # fix 4
    a, b, la, lb = _setup()
    line = {'type': 'detail', 'name': 'Фланец', 'material_key': 'lot_b', 'qty': 10, 'unit_price': None}
    html = _receipt(admin_client, a, [line, _line(2)]).get_data(as_text=True)   # the good line is saved
    assert 'не беше записан' in html and Detail.query.filter_by(name='Фланец').count() == 0
    assert DeliveryNote.query.count() == 1


def test_delete_material_refusals(admin_client):   # fix 5
    a, b, la, lb = _setup()
    html = admin_client.post('/admin/materials/lot_a/delete', follow_redirects=True).get_data(as_text=True)
    assert 'Партида на клиент с наличност не може да се изтрие.' in html
    assert db.session.get(MaterialPrice, la) is not None
    # a remnant lot that a note line still points at
    _out(admin_client, a, 'consume', [_mline(la, 2, remnant_qty=1, remnant_width=600, remnant_height=900)])
    rem = MaterialPrice.query.filter_by(display_name='Стомана 2мм - остатък').one()
    rem_id, rem_key = rem.id, rem.key
    rem.stock_quantity = 0
    db.session.commit()
    html = admin_client.post(f'/admin/materials/{rem_key}/delete', follow_redirects=True).get_data(as_text=True)
    assert 'не може да бъде изтрит' in html and db.session.get(MaterialPrice, rem_id) is not None


def test_option_text_owner_row_missing(admin_client):   # fix 7
    m = MaterialPrice(key='z', display_name='Мед', cost_per_m2=0, type='sheets', owner_client_id=999)
    assert format_material_option(m).endswith(' [клиент: #999]')
