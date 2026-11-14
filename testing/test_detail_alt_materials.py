"""
pytest: one Detail, several materials. A Detail keeps its default material
(Detail.material_key) plus a list of other allowed ones (DetailMaterial,
edited on the detail page). The material is picked per standalone-detail order
line (OrderItem.material_key, NULL = default); price, reservation, consumption
and the sheet remnant then follow the chosen lot (_line_material()).

Run with:
    pytest testing/test_detail_alt_materials.py -v
"""
import json
import re
import shutil
import subprocess

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Service, Detail, DetailMaterial, Operation,
                 Order, OrderItem, OrderMaterialReservation, limiter, seed_material_types,
                 calculate_material_price, detail_price_for, _detail_total_price, _material_stock_delta)

PASSWORD = 'irrelevant123'


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        # Материали discount (baked into an owned detail's price), Детайли markup (on everything else).
        client_a = Client(name='Клиент А', material_adjustment_type='discount', material_adjustment_percent=10,
                          detail_adjustment_type='markup', detail_adjustment_percent=7.5,
                          own_material_surcharge_percent=20)
        client_b = Client(name='Клиент Б')
        svc = Service(name='Огъване', price_per_hour_eur=61.7)
        db.session.add_all([client_a, client_b, svc])
        db.session.flush()
        sheet = dict(type='sheets', sheet_width_mm=1000, sheet_length_mm=2000)
        db.session.add_all([
            MaterialPrice(key='steel', display_name='Стомана', cost_per_m2=100, stock_quantity=10, **sheet),
            MaterialPrice(key='alu', display_name='Алуминий', cost_per_m2=333.33, stock_quantity=10, **sheet),
            MaterialPrice(key='inox', display_name='Неръждаема', cost_per_m2=250, stock_quantity=10, **sheet),
            MaterialPrice(key='steel_a', display_name='Стомана клиент', cost_per_m2=0, stock_quantity=5,
                          owner_client_id=client_a.id, **sheet),
            MaterialPrice(key='alu_b', display_name='Алуминий клиент Б', cost_per_m2=0, stock_quantity=5,
                          owner_client_id=client_b.id, **sheet),
        ])
        db.session.flush()

        def detail(name, owner=None, width=500, height=400):
            d = Detail(name=name, material_key='steel', width=width, height=height, total_length=1, pierce_count=1,
                       calculated_price=calculate_material_price(width, height, 'steel', owner), owner_client_id=owner and owner.id)
            db.session.add(d)
            db.session.flush()
            db.session.add(Operation(detail_id=d.id, service_id=svc.id, duration_minutes=7.3))
            return d

        public = detail('Планка')
        private = detail('Планка А', owner=client_a)
        manual = detail('Ръчна', width=0, height=0)
        db.session.add_all([DetailMaterial(detail_id=public.id, material_key='alu'),
                            DetailMaterial(detail_id=private.id, material_key='alu'),
                            DetailMaterial(detail_id=private.id, material_key='steel_a')])
        pw = generate_password_hash(PASSWORD)
        db.session.add_all([User(username='boss', password=pw, role='admin'),
                            User(username='customer', password=pw, role='regular_user', client_id=client_a.id)])
        db.session.commit()
        yield dict(a=client_a.id, b=client_b.id, svc=svc.id, public=public.id, private=private.id, manual=manual.id,
                   **{m.key: m.id for m in MaterialPrice.query})
        db.session.remove()
        db.drop_all()


def _login(username):
    limiter.reset()
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': PASSWORD})
    return c


def _alts(detail_id):
    with flask_app.app_context():
        return sorted(dm.material_key for dm in DetailMaterial.query.filter_by(detail_id=detail_id))


def _stock(material_id):
    with flask_app.app_context():
        return db.session.get(MaterialPrice, material_id).stock_quantity


def _order(c, cart, client_id=None):
    data = {'customer_name': 'QA', 'cart_json': json.dumps(cart)}
    if client_id:
        data['client_id'] = str(client_id)
    return c.post('/orders/new', data=data, follow_redirects=True)


def _last_order_items():
    with flask_app.app_context():
        order = Order.query.order_by(Order.id.desc()).first()
        if not order:
            return None, []
        return order.id, [(i.id, i.material_key, i.unit_price) for i in OrderItem.query.filter_by(order_id=order.id)
                          .order_by(OrderItem.id)]


def _reservations(order_id):
    with flask_app.app_context():
        return {r.material_id: r.quantity for r in OrderMaterialReservation.query.filter_by(order_id=order_id)}


def test_add_and_remove_allowed_material(ids):
    c = _login('boss')
    url = f'/details/{ids["public"]}/materials/add'
    assert 'вече е разрешен' in c.post(url, data={'material_key': 'alu'}, follow_redirects=True).get_data(as_text=True)
    assert 'вече е разрешен' in c.post(url, data={'material_key': 'steel'}, follow_redirects=True).get_data(as_text=True)
    for key in ('alu_b', 'steel_a', 'nope'):   # a client's lot on a public detail, an unknown key
        assert 'Невалиден избор' in c.post(url, data={'material_key': key}, follow_redirects=True).get_data(as_text=True)
    c.post(url, data={'material_key': 'inox'})
    assert _alts(ids['public']) == ['alu', 'inox']
    # a private detail: our lots + the owner's, never another client's
    c.post(f'/details/{ids["private"]}/materials/add', data={'material_key': 'alu_b'})
    assert _alts(ids['private']) == ['alu', 'steel_a']
    # a manual-price detail has no size to price another material from
    c.post(f'/details/{ids["manual"]}/materials/add', data={'material_key': 'alu'})
    assert _alts(ids['manual']) == []

    page = c.get(f'/details/{ids["public"]}/files').get_data(as_text=True)
    assert 'Други разрешени материали' in page and 'Неръждаема' in page
    with flask_app.app_context():
        row_id = DetailMaterial.query.filter_by(detail_id=ids['public'], material_key='inox').one().id
    c.post(f'/details/{ids["public"]}/materials/{row_id}/delete')
    assert _alts(ids['public']) == ['alu']

    # making an allowed material the default drops it from the list (never listed twice)
    c.post(f'/details/{ids["public"]}/update-material', data={'material': 'alu', 'width': '500', 'height': '400'})
    assert _alts(ids['public']) == []


def test_price_per_material_is_the_engines_price(ids):
    with flask_app.app_context():
        client_a = db.session.get(Client, ids['a'])
        public, private = db.session.get(Detail, ids['public']), db.session.get(Detail, ids['private'])
        ops = sum(op.cost for op in public.operations)
        prices = {}
        for key in ('steel', 'alu'):
            # the price of a Detail freshly made with that material (admin_add_detail()'s pricing)
            fresh = round(calculate_material_price(500, 400, key) + ops, 2)
            assert _detail_total_price(public, MaterialPrice.query.filter_by(key=key).one()) == fresh
            assert detail_price_for(public, None, False, key) == fresh
            prices[key] = detail_price_for(public, client_a, False, key)
            assert prices[key] == round(fresh * 1.075, 2)   # Детайли markup on top
        assert prices['steel'] != prices['alu']
        assert detail_price_for(public, client_a, False, None) == prices['steel']   # default unchanged
        assert detail_price_for(public, client_a, True, 'alu') == detail_price_for(public, client_a, True)  # own material
        # owned detail: the owner's Материали terms are in the material cost, no Детайли on top
        assert detail_price_for(private, client_a, False, 'alu') == round(calculate_material_price(500, 400, 'alu', client_a) + ops, 2)
        # the owner's own stored lot: no material, operations x his surcharge
        assert detail_price_for(private, client_a, False, 'steel_a') == round(ops * 1.2, 2)
        with pytest.raises(ValueError):
            detail_price_for(public, client_a, False, 'inox')   # not allowed for this detail


def test_customer_order_freezes_the_chosen_material_price(ids):
    c = _login('customer')
    body = c.get('/orders/new').get_data(as_text=True)
    details = {d['id']: d for d in json.loads(re.search(r'const DETAILS = (.*);$', body, re.M).group(1))}
    shown = details[ids['public']]
    # finished prices only: nothing to derive the cost structure from
    assert set(shown) == {'id', 'name', 'price', 'price_own', 'materials'}
    assert [set(m) for m in shown['materials']] == [{'key', 'name', 'price'}] * 2
    assert [m['key'] for m in shown['materials']] == ['steel', 'alu']
    assert 'materials' not in details[ids['manual']]
    for leaked in ('ops_total', 'detail_percent', 'markup_percent'):
        assert f'"{leaked}"' not in body
    alu_price = shown['materials'][1]['price']

    # the browser's price and client id change nothing
    _order(c, [{'type': 'detail', 'id': ids['public'], 'qty': 3, 'material_key': 'alu', 'unit_price': 0.01},
               {'type': 'detail', 'id': ids['public'], 'qty': 2}], client_id=ids['b'])
    _, items = _last_order_items()
    assert [(key, price) for _, key, price in items] == [('alu', alu_price), (None, shown['price'])]
    with flask_app.app_context():
        assert alu_price == detail_price_for(db.session.get(Detail, ids['public']), db.session.get(Client, ids['a']),
                                             False, 'alu')


@pytest.mark.parametrize('key', ['inox', 'alu_b', 'nope', 5])
def test_an_invalid_material_refuses_the_order(ids, key):
    c = _login('boss')
    res = _order(c, [{'type': 'detail', 'id': ids['public'], 'qty': 1, 'material_key': key}])
    assert 'Невалиден материал' in res.get_data(as_text=True)
    assert _last_order_items() == (None, [])


def test_the_chosen_lot_is_reserved_consumed_and_remnanted(ids):
    c = _login('boss')
    _order(c, [{'type': 'detail', 'id': ids['public'], 'qty': 7, 'material_key': 'alu'}], client_id=ids['a'])
    order_id, [(item_id, key, _)] = _last_order_items()
    assert key == 'alu'
    with flask_app.app_context():
        need = _material_stock_delta(db.session.get(MaterialPrice, ids['alu']), 0.2 * 7)   # 0.7 of a 2 m² sheet
    assert _reservations(order_id) == {ids['alu']: pytest.approx(need)}

    res = c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '7'}).get_json()
    assert res['status'] == 'success'
    assert _stock(ids['alu']) == pytest.approx(10 - 0.7) and _stock(ids['steel']) == 10
    with flask_app.app_context():
        assert db.session.get(OrderItem, item_id).consumed_material_id == ids['alu']
    assert res['remnant_prompt']['material_id'] == ids['alu'] and res['remnant_prompt']['sheets'] == 1

    res = c.post('/admin/production/remnants', data={'order_id': order_id, 'material_id': ids['alu'],
                                                     'sheets': json.dumps([{'action': 'scrap'}])}).get_json()
    assert res['status'] == 'success'
    assert _stock(ids['alu']) == pytest.approx(9) and _stock(ids['steel']) == 10

    page = c.get('/admin/production').get_data(as_text=True)
    assert 'Материал: #' in page and 'Алуминий' in page
    assert 'ORD' in c.get(f'/admin/materials/{ids["alu"]}/history').get_data(as_text=True)


def test_default_material_behaves_as_before(ids):
    c = _login('boss')
    _order(c, [{'type': 'detail', 'id': ids['public'], 'qty': 5}], client_id=ids['a'])
    order_id, [(item_id, key, price)] = _last_order_items()
    with flask_app.app_context():
        assert key is None
        assert price == detail_price_for(db.session.get(Detail, ids['public']), db.session.get(Client, ids['a']))
    assert set(_reservations(order_id)) == {ids['steel']}
    c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '5'})
    assert _stock(ids['steel']) == pytest.approx(10 - 0.5) and _stock(ids['alu']) == 10


def test_changing_the_material_before_and_after_production(ids):
    c = _login('boss')
    _order(c, [{'type': 'detail', 'id': ids['public'], 'qty': 5,
                'operations': [{'service_id': ids['svc'], 'duration_minutes': 10}]}], client_id=ids['a'])
    order_id, [(item_id, _, steel_line)] = _last_order_items()
    assert 'Смени материала' in c.get('/admin/production').get_data(as_text=True)

    c.post(f'/admin/order-items/{item_id}/material', data={'material_key': 'inox'})   # not allowed
    assert _last_order_items()[1][0][1] is None

    c.post(f'/admin/order-items/{item_id}/material', data={'material_key': 'alu'})
    _, [(_, key, alu_line)] = _last_order_items()
    with flask_app.app_context():
        detail, client = db.session.get(Detail, ids['public']), db.session.get(Client, ids['a'])
        delta = detail_price_for(detail, client, False, 'alu') - detail_price_for(detail, client)
    assert key == 'alu' and alu_line == round(steel_line + delta, 2)   # the order-time operation stays
    assert set(_reservations(order_id)) == {ids['alu']}   # the reservation moved to the new lot

    c.post('/admin/production', data={'target_type': 'item', 'target_id': item_id, 'produced_qty': '1'})
    res = c.post(f'/admin/order-items/{item_id}/material', data={'material_key': 'steel'}, follow_redirects=True)
    assert 'Производството по този ред е започнало' in res.get_data(as_text=True)
    assert _last_order_items()[1][0][1] == 'alu'


NODE_HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const document = { getElementById: function () { return { value: input.client }; } };
const page = new Function('document', fs.readFileSync(process.argv[2], 'utf8') +
    '\nreturn { liveUnitPrice: liveUnitPrice };')(document);
process.stdout.write(JSON.stringify(input.items.map(page.liveUnitPrice)));
"""


@pytest.mark.parametrize('username, client_key', [('boss', None), ('boss', 'a'), ('boss', 'b'), ('customer', 'a')])
def test_page_js_previews_what_the_order_freezes(ids, tmp_path, username, client_key):
    node = shutil.which('node')
    if not node:
        pytest.skip('node не е инсталиран')
    c = _login(username)
    body = c.get('/orders/new').get_data(as_text=True)
    client_id = ids[client_key] if client_key else None
    cart = [{'type': 'detail', 'id': d, 'qty': 2, 'material_key': k, 'own_material': own}
            for d, keys in ((ids['public'], ('alu', None)), (ids['private'], ('alu', 'steel_a', None)))
            for k in keys for own in (False, True)]
    (tmp_path / 'harness.js').write_text(NODE_HARNESS, encoding='utf-8')
    (tmp_path / 'page.js').write_text(body[body.index('const PRODUCTS = '):body.index('function addPendingOperation')],
                                      encoding='utf-8')
    items = [{'type': 'detail', 'id': r['id'], 'ownMaterial': r['own_material'], 'materialKey': r['material_key'],
              'operations': []} for r in cart]
    (tmp_path / 'input.json').write_text(json.dumps({'client': str(client_id or ''), 'items': items}), encoding='utf-8')
    run = subprocess.run([node, str(tmp_path / 'harness.js'), str(tmp_path / 'page.js'), str(tmp_path / 'input.json')],
                         capture_output=True, text=True, encoding='utf-8')
    assert run.returncode == 0, run.stderr
    previewed = json.loads(run.stdout)
    _order(c, [{k: v for k, v in r.items() if v is not None} for r in cart], client_id)
    frozen = [price for _, _, price in _last_order_items()[1]]
    assert previewed == frozen
