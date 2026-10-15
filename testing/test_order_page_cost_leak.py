"""
pytest: /orders/new must not hand a customer the shop's cost structure, and
the price the page previews must be the price the order freezes.

The page's JS used to get every product's markup_percent / details_subtotal /
extra_costs_subtotal (and each detail's material-vs-services split) for every
viewer, because it recomputed prices in the browser. A customer can only order
as their own client, so they now get finished prices (shop material / own
material) computed by calculate_product_pricing() / detail_price_for(); staff
keep the breakdown and the JS re-runs the formula for the client they pick.

The parity half runs the page's own pricing functions under node (skipped
when node is not installed) against the unit_price create_order() stores.

Run with:
    pytest testing/test_order_page_cost_leak.py -v
"""
import atexit
import json
import os
import random
import re
import shutil
import subprocess
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _safe_remove():
    try:
        os.remove(_db_path)
    except OSError:
        pass


atexit.register(_safe_remove)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, ClientServicePrice, MaterialPrice, Service, Detail, Operation,
                 Product, ProductDetail, ProductExtraCost, Order, OrderItem, limiter,
                 detail_price_for, calculate_product_pricing)

PASSWORD = 'irrelevant123'
# Order-time operations put on a standalone-Detail cart line (the second one is
# filled in with the hidden-price service's id by the fixture).
OPS = [{'duration_minutes': 7.5}, {'duration_minutes': 12.34}]


@pytest.fixture(scope='module')   # one catalog for the whole file - no test changes it, they only add orders
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        rnd = random.Random(7)
        mat = MaterialPrice(key='steel', display_name='Стомана', type='sheets', cost_per_m2=100,
                            cutting_speed_mm_per_min=1000, pierce_rate_per_min=60)
        # Продукти discount, Детайли markup, own-material surcharge, one cheaper service.
        client_a = Client(name='Клиент А', product_adjustment_type='discount', product_adjustment_percent=12.5,
                          detail_adjustment_type='markup', detail_adjustment_percent=7.5,
                          own_material_surcharge_percent=17.5)
        client_b = Client(name='Клиент Б', product_adjustment_type='markup', product_adjustment_percent=2.5,
                          detail_adjustment_type='discount', detail_adjustment_percent=12.5)
        svc_time = Service(name='Огъване', price_per_hour_eur=61.7)
        svc_len = Service(name='Лазер', price_per_hour_eur=80, pricing_mode='length', price_per_meter_eur=3.33)
        svc_hidden = Service(name='Заваряване', price_per_hour_eur=97, show_price=False)
        db.session.add_all([mat, client_a, client_b, svc_time, svc_len, svc_hidden])
        db.session.flush()
        db.session.add(ClientServicePrice(client_id=client_a.id, service_id=svc_time.id,
                                          adjustment_type='discount', adjustment_percent=15))

        def detail(name, price, ops=()):
            d = Detail(name=name, material_key='steel', width=100, height=100, total_length=0, pierce_count=0,
                       calculated_price=price)
            db.session.add(d)
            db.session.flush()
            for svc, minutes, length in ops:
                db.session.add(Operation(detail_id=d.id, service_id=svc.id, duration_minutes=minutes, length_mm=length))
            return d

        def product(name, markup, parts, extras):
            p = Product(name=name, markup_percent=markup)
            db.session.add(p)
            db.session.flush()
            db.session.add_all([ProductDetail(product_id=p.id, detail_id=d.id, quantity=q) for d, q in parts])
            db.session.add_all([ProductExtraCost(product_id=p.id, label='Боя', amount=a) for a in extras])
            return p

        # A readable handful first...
        d1 = detail('Плоча', 13.37, [(svc_time, 7, None), (svc_len, 0, 1234)])
        d2 = detail('Скоба', 4.105, [(svc_time, 3.3, None)])
        d3 = detail('Шайба', 9.99)
        details = [d1, d2, d3]
        product('Шкаф', 27.5, [(d1, 3), (d2, 7)], [4.44, 0.615])
        product('Услуга без детайли', 0, [], [12.125])     # an exact half-cent tie
        product('Стойка', 12.5, [(d3, 1)], [])
        # ...then a seeded batch full of x.xx5 amounts, where a rounding done
        # at a different point than the server's shows up as a cent.
        for i in range(12):
            details.append(detail(f'Детайл {i}', rnd.randrange(100, 90000, 5) / 1000,
                                  [(rnd.choice([svc_time, svc_hidden]), rnd.randrange(5, 4000, 5) / 100, None)
                                   for _ in range(rnd.randrange(3))]))
        for i in range(9):
            product(f'Продукт {i}', rnd.choice([0, 2.5, 12.5, 33, 17.75]),
                    [(d, rnd.randrange(1, 40)) for d in rnd.sample(details, rnd.randrange(1, 4))],
                    [rnd.randrange(5, 30000, 5) / 1000 for _ in range(rnd.randrange(3))])

        pw = generate_password_hash(PASSWORD)
        db.session.add_all([User(username='customer', password=pw, role='regular_user', client_id=client_a.id),
                            User(username='unlinked', password=pw, role='regular_user'),
                            User(username='boss', password=pw, role='admin')])
        db.session.commit()
        OPS[0]['service_id'], OPS[1]['service_id'] = svc_time.id, svc_hidden.id
        found = dict(a=client_a.id, b=client_b.id, hidden=svc_hidden.id,
                     products=[p.id for p in Product.query.order_by(Product.id)],
                     details=[d.id for d in Detail.query.order_by(Detail.id)])
    # No app context stays open around the requests: flask-login caches the user
    # on g, and a context shared by two test clients would mix their logins up.
    yield found
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


def _page(username):
    limiter.reset()   # /login is limited to 10 per minute
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': PASSWORD})
    body = c.get('/orders/new').get_data(as_text=True)
    return c, body


def _embedded(body, name):
    return json.loads(re.search(r'const %s = (.*);$' % name, body, re.M).group(1))


def _cart(ids):
    """Every product and detail, with and without own material; details also with order-time operations."""
    rows = []
    for own in (False, True):
        rows += [{'type': 'product', 'id': i, 'qty': 3, 'own_material': own} for i in ids['products']]
        for ops in ([], OPS):
            rows += [{'type': 'detail', 'id': i, 'qty': 3, 'own_material': own, 'operations': ops}
                     for i in ids['details']]
    return rows


def _order(c, cart, client_id=None):
    """POSTs the cart and returns the unit prices create_order() froze, in cart order."""
    data = {'customer_name': 'QA', 'cart_json': json.dumps(cart)}
    if client_id:
        data['client_id'] = str(client_id)
    c.post('/orders/new', data=data)
    with flask_app.app_context():
        order = Order.query.order_by(Order.id.desc()).first()
        items = OrderItem.query.filter_by(order_id=order.id).order_by(OrderItem.id).all()
        assert len(items) == len(cart)
        return [i.unit_price for i in items]


def test_customer_page_has_no_cost_structure(ids):
    _, body = _page('customer')
    # (as a JSON key - the page's script still names these fields in the staff branch of its code)
    for leaked in ('markup_percent', 'details_subtotal', 'extra_costs_subtotal', 'details_ops', 'ops_total',
                   'product_percent', 'detail_percent', 'product_type', 'detail_type'):
        assert f'"{leaked}"' not in body, f'{leaked} must not reach a customer'
    # Nothing but finished prices: no field left to derive the margin from.
    assert all(set(p) == {'id', 'name', 'price', 'price_own'} for p in _embedded(body, 'PRODUCTS'))
    assert all(set(d) == {'id', 'name', 'price', 'price_own'} for d in _embedded(body, 'DETAILS'))
    pricing = _embedded(body, 'CLIENT_PRICING')
    assert list(pricing) == [str(ids['a'])], 'only the viewer\'s own client'
    assert set(pricing[str(ids['a'])]) == {'own_material_percent', 'services'}
    # The list price (before this client's terms) of the first product is not on the page either.
    with flask_app.app_context():
        product = db.session.get(Product, ids['products'][0])
        list_price = calculate_product_pricing(product)['sell_price']
        assert list_price != calculate_product_pricing(product, db.session.get(Client, ids['a']))['sell_price']
    assert str(list_price) not in body


def test_staff_page_keeps_the_breakdown(ids):
    _, body = _page('boss')
    products = _embedded(body, 'PRODUCTS')
    assert all({'markup_percent', 'details_subtotal', 'extra_costs_subtotal', 'details_ops', 'price'} <= set(p)
               for p in products)
    assert set(_embedded(body, 'CLIENT_PRICING')) == {str(ids['a']), str(ids['b'])}
    with flask_app.app_context():
        for p in products:   # 'price' is still the plain list price
            assert p['price'] == calculate_product_pricing(db.session.get(Product, p['id']))['sell_price']


@pytest.mark.parametrize('username, client_key', [('customer', 'a'), ('unlinked', None)])
def test_customer_prices_are_the_server_functions_results(ids, username, client_key):
    _, body = _page(username)
    with flask_app.app_context():
        client = db.session.get(Client, ids[client_key]) if client_key else None
        products = _embedded(body, 'PRODUCTS')
        assert [p['id'] for p in products] and len(products) == len(ids['products'])
        for p in products:
            product = db.session.get(Product, p['id'])
            assert p['price'] == calculate_product_pricing(product, client)['sell_price']
            assert p['price_own'] == calculate_product_pricing(product, client, True)['sell_price']
        details = _embedded(body, 'DETAILS')
        assert len(details) == len(ids['details'])
        for d in details:
            detail = db.session.get(Detail, d['id'])
            assert d['price'] == detail_price_for(detail, client)
            assert d['price_own'] == detail_price_for(detail, client, True)


def test_customer_order_freezes_the_embedded_prices(ids):
    c, body = _page('customer')
    shown = {('product', p['id']): p for p in _embedded(body, 'PRODUCTS')}
    shown.update({('detail', d['id']): d for d in _embedded(body, 'DETAILS')})
    cart = [row for row in _cart(ids) if not row.get('operations')]
    # A price or another client's id sent from the browser changes nothing.
    for row in cart:
        row['unit_price'] = row['price'] = 0.01
    frozen = _order(c, cart, client_id=ids['b'])
    for row, unit_price in zip(cart, frozen):
        assert unit_price == shown[(row['type'], row['id'])]['price_own' if row['own_material'] else 'price'], row


NODE_HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
// the one thing the pricing functions read from the page: the client picker
const document = { getElementById: function () { return { value: input.client }; } };
const page = new Function('document', fs.readFileSync(process.argv[2], 'utf8') +
    '\nreturn { liveUnitPrice: liveUnitPrice, round2: round2 };')(document);
process.stdout.write(JSON.stringify({
    prices: input.items.map(page.liveUnitPrice),
    rounded: input.numbers.map(page.round2),
}));
"""


def _run_page_js(tmp_path, body, client_id, cart, numbers=()):
    """Runs the pricing functions of the rendered page (everything from the
    embedded catalogs down to the first DOM-only function) under node."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node не е инсталиран - сверката на JS цените със сървъра е пропусната')
    (tmp_path / 'harness.js').write_text(NODE_HARNESS, encoding='utf-8')
    (tmp_path / 'page.js').write_text(
        body[body.index('const PRODUCTS = '):body.index('function addPendingOperation')], encoding='utf-8')
    items = [{'type': r['type'], 'id': r['id'], 'ownMaterial': r['own_material'],
              'operations': [{'serviceId': o['service_id'], 'durationMinutes': o['duration_minutes']}
                             for o in r.get('operations', [])]} for r in cart]
    (tmp_path / 'input.json').write_text(
        json.dumps({'client': str(client_id or ''), 'items': items, 'numbers': list(numbers)}), encoding='utf-8')
    run = subprocess.run([node, str(tmp_path / 'harness.js'), str(tmp_path / 'page.js'), str(tmp_path / 'input.json')],
                         capture_output=True, text=True, encoding='utf-8')
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize('username, client_key', [
    ('boss', None), ('boss', 'a'), ('boss', 'b'),   # staff: the JS re-runs the formula per picked client
    ('customer', 'a'), ('unlinked', None),          # customer: the JS only picks a finished price
])
def test_page_js_previews_exactly_what_the_order_freezes(ids, tmp_path, username, client_key):
    c, body = _page(username)
    client_id = ids[client_key] if client_key else None
    cart = _cart(ids)
    previewed = _run_page_js(tmp_path, body, client_id, cart)['prices']
    frozen = _order(c, cart, client_id)
    mismatches = [(row, js, py) for row, js, py in zip(cart, previewed, frozen) if js != py]
    assert not mismatches, mismatches[:5]
    # ...and what was frozen is what the pricing helpers say (rows without order-time operations).
    with flask_app.app_context():
        client = db.session.get(Client, client_id) if client_id else None
        for row, unit_price in zip(cart, frozen):
            if row['type'] == 'product':
                assert unit_price == calculate_product_pricing(db.session.get(Product, row['id']), client,
                                                               row['own_material'])['sell_price']
            elif not row['operations']:
                assert unit_price == detail_price_for(db.session.get(Detail, row['id']), client, row['own_material'])


def test_js_round2_is_pythons_round(ids, tmp_path):
    rnd = random.Random(11)
    numbers = [k / 8 for k in range(-40, 400)]                               # exact ties -> even cent
    numbers += [n / 1000 for n in range(5, 20000, 10)]                       # x.xx5, not exactly representable
    numbers += [rnd.uniform(0, 5000) for _ in range(2000)]
    numbers += [rnd.randrange(0, 5000000) / 1000 * rnd.choice([0.875, 1.125, 1.175, 1.275]) for _ in range(2000)]
    _, body = _page('boss')
    rounded = _run_page_js(tmp_path, body, None, [], numbers)['rounded']
    wrong = [(x, js, round(x, 2)) for x, js in zip(numbers, rounded) if js != round(x, 2)]
    assert not wrong, wrong[:5]
