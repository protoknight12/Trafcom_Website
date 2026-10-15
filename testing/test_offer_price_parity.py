"""
pytest: the offer pages price exactly like the server, to the cent.

Server facts guarded here:
- _save_offer() stores each line's posted unit_price as is - it never
  recomputes a catalog price (the editor's JS only pre-fills the field).
- admin_offer_print() shows the stored prices; OfferItem.line_total,
  Offer.subtotal/discount_amount/total each round to the cent.
- admin_offer_create_order() freezes the QUOTED line price minus the offer's
  whole-offer discount (owner's decision 2026-10-15), never a fresh catalog
  price.

The node cross-check runs the editor's own pricing JS (admin_offer_edit.html)
and offer.html's recomputeOfferPricing() on the JSON the server embeds, for
a random catalog and several clients, and compares every figure with the
server. Skipped when node is not installed.

Run with:
    pytest testing/test_offer_price_parity.py -q
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


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked as long as SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, ClientServicePrice, Service, MaterialPrice, Detail,
                  Operation, Product, ProductDetail, ProductExtraCost, Offer, OfferItem, Order, OrderItem,
                  calculate_product_pricing, detail_price_for, limiter)

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')
MARKUPS = [0, 10, 12.5, 15, 17.3, 20, 25, 33.33, 7.77]


def build_catalog(seed=1, n_details=40, n_products=60):
    """Random details (with operations) and products, plus clients covering
    every kind of term. Returns (product_ids, detail_ids, client_ids)."""
    rnd = random.Random(seed)
    s1 = Service(name='QA Laser', price_per_hour_eur=47.3)
    s2 = Service(name='QA Bend', price_per_hour_eur=38.9)
    material = MaterialPrice(key='qa_parity', display_name='QA Parity', type='sheets', cost_per_m2=13.37)
    db.session.add_all([s1, s2, material])
    db.session.flush()
    details = []
    for i in range(n_details):
        d = Detail(name=f'QA D{i}', material_key=material.key, width=100, height=100, total_length=0,
                   pierce_count=0, calculated_price=round(rnd.uniform(0.2, 90), rnd.choice([2, 3, 4])))
        db.session.add(d)
        db.session.flush()
        for seq in range(rnd.randint(0, 2)):
            db.session.add(Operation(detail_id=d.id, service_id=rnd.choice([s1, s2]).id, sequence=seq,
                                     duration_minutes=round(rnd.uniform(0.1, 9), 2)))
        details.append(d)
    products = []
    for i in range(n_products):
        p = Product(name=f'QA P{i}', markup_percent=rnd.choice(MARKUPS))
        db.session.add(p)
        db.session.flush()
        for d in rnd.sample(details, rnd.randint(1, 4)):
            db.session.add(ProductDetail(product_id=p.id, detail_id=d.id, quantity=rnd.randint(1, 7)))
        for k in range(rnd.randint(0, 2)):
            db.session.add(ProductExtraCost(product_id=p.id, label=f'X{k}', amount=round(rnd.uniform(1, 60), 2)))
        products.append(p)
    clients = [
        Client(name='QA all terms', product_adjustment_type='discount', product_adjustment_percent=12.5,
               detail_adjustment_type='markup', detail_adjustment_percent=7.5, own_material_surcharge_percent=15),
        Client(name='QA odd', product_adjustment_type='markup', product_adjustment_percent=3.3,
               detail_adjustment_type='discount', detail_adjustment_percent=17),
        Client(name='QA third', product_adjustment_type='discount', product_adjustment_percent=33.33,
               detail_adjustment_type='discount', detail_adjustment_percent=1),
        Client(name='QA none'),
    ]
    db.session.add_all(clients)
    db.session.flush()
    db.session.add(ClientServicePrice(client_id=clients[0].id, service_id=s1.id,
                                      adjustment_type='discount', adjustment_percent=10))
    db.session.commit()
    return [p.id for p in products], [d.id for d in details], [c.id for c in clients]


@pytest.fixture(scope='module')
def catalog():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_parity_admin', password=generate_password_hash('irrelevant123'),
                            role='admin'))
        db.session.commit()
        ids = build_catalog()
        yield ids
        db.session.remove()
        db.drop_all()


@pytest.fixture
def browser(catalog):
    limiter.reset()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_parity_admin', 'password': 'irrelevant123'})
    return c


def _items_json(rows):
    return json.dumps([dict({'code': '', 'description_html': '', 'dimensions': '', 'unit': 'бр',
                             'image_filename': ''}, **r) for r in rows])


def test_save_offer_stores_posted_price_verbatim(browser, catalog):
    product_ids, detail_ids, client_ids = catalog
    res = browser.post('/admin/offers/new', data={'client_id': str(client_ids[0]), 'items_json': _items_json([
        {'type': 'product', 'name': 'QA P0', 'product_id': product_ids[0], 'quantity': 3, 'unit_price': 12.345},
        {'type': 'detail', 'name': 'QA D0', 'detail_id': detail_ids[0], 'quantity': 2, 'unit_price': 0.5},
    ])})
    assert res.status_code == 302
    with flask_app.app_context():
        offer = Offer.query.order_by(Offer.id.desc()).first()
        assert [i.unit_price for i in offer.items] == [12.345, 0.5], 'typed prices are stored untouched'


def test_print_rounds_lines_subtotal_discount_and_total(browser):
    res = browser.post('/admin/offers/new', data={'discount_percent': '12.345', 'items_json': _items_json([
        {'type': 'text', 'name': 'A', 'quantity': 3, 'unit_price': 0.335},     # 1.0050000000000001 -> 1.01
        {'type': 'text', 'name': 'B', 'quantity': 1, 'unit_price': 0.125},     # exact tie -> 0.12
        {'type': 'text', 'name': 'C', 'quantity': 2.5, 'unit_price': 41.17},   # 102.92500000000001 -> 102.93
        {'type': 'text', 'name': 'note only'},
    ])})
    assert res.status_code == 302
    with flask_app.app_context():
        offer = Offer.query.order_by(Offer.id.desc()).first()
        lines = [round((i.quantity or 0) * (i.unit_price or 0), 2) for i in offer.items]
        assert [i.line_total for i in offer.items] == lines
        assert offer.subtotal == round(sum(lines), 2) == 104.06
        assert offer.discount_amount == round(104.06 * 12.345 / 100, 2) == 12.85
        assert offer.total == 91.21
    page = browser.get(f'/admin/offers/{offer.id}/print').get_data(as_text=True)
    for shown in ('1.01', '0.12', '102.93', '104.06 EUR', '-12.85 EUR', '91.21 EUR'):
        assert shown in page


def test_create_order_freezes_quoted_price_minus_offer_discount(browser, catalog):
    product_ids, detail_ids, client_ids = catalog
    browser.post('/admin/offers/new', data={'client_id': str(client_ids[0]), 'discount_percent': '10',
                                            'items_json': _items_json([
        {'type': 'product', 'name': 'QA P1', 'product_id': product_ids[1], 'quantity': 2, 'unit_price': 1.11},
        {'type': 'detail', 'name': 'QA D1', 'detail_id': detail_ids[1], 'quantity': 4, 'unit_price': 2.22},
    ])})
    with flask_app.app_context():
        offer = Offer.query.order_by(Offer.id.desc()).first()
        item_ids = [str(i.id) for i in offer.items]
    browser.post(f'/admin/offers/{offer.id}/create-order', data={'customer_name': 'QA Parity Order',
                                                                 'item_ids': item_ids})
    with flask_app.app_context():
        client = db.session.get(Client, client_ids[0])
        order = Order.query.filter_by(customer_name='QA Parity Order').one()
        by_product = {i.product_id: i for i in OrderItem.query.filter_by(order_id=order.id)}
        product_line = by_product[product_ids[1]]
        detail_line = next(i for i in by_product.values() if i.detail_id == detail_ids[1])
        # quoted 1.11 / 2.22 less the offer's 10%, to the cent - not the client's fresh catalog price
        assert product_line.unit_price == round(1.11 * 0.9, 2) == 1.0
        assert detail_line.unit_price == round(2.22 * 0.9, 2) == 2.0
        assert product_line.unit_price != calculate_product_pricing(db.session.get(Product, product_ids[1]),
                                                                    client)['sell_price']


# ---------------- node cross-check ----------------

def _js_function(src, name):
    """The source of `function name(...) {...}` from a template (brace matching)."""
    start = src.index(f'function {name}(')
    depth, i = 0, src.index('{', start)
    while True:
        depth += {'{': 1, '}': -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


def _embedded(page, name):
    return re.search(rf'const {name} = (.*);$', page, re.M).group(1)


DOM_STUB = """
const ELEMENTS = {};
const document = { getElementById: function (id) {
    return ELEMENTS[id] || (ELEMENTS[id] = { value: '', textContent: '', style: {} });
} };
"""


def _node(script):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(script)
    try:
        out = subprocess.run(['node', f.name], capture_output=True, text=True, encoding='utf-8', timeout=60)
    finally:
        os.remove(f.name)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(not shutil.which('node'), reason='node is not installed - JS parity not checked')


def editor_js(src, page):
    """The editor's pricing JS with the catalog JSON the server embedded."""
    names = ['applyClientAdjustment', 'selectedOfferClientPricing', 'detailAdjustedPrice',
             'productAdjustedPrice', 'round2', 'lineTotal', 'offerTotals']
    return DOM_STUB + ''.join(f'const {n} = {_embedded(page, n)};\n' for n in ('PRODUCTS', 'DETAILS', 'CLIENT_PRICING')) \
        + '\n'.join(_js_function(src, n) for n in names if f'function {n}(' in src)


def editor_mismatches(browser, catalog, src):
    product_ids, detail_ids, client_ids = catalog
    page = browser.get('/admin/offers/new').get_data(as_text=True)
    js = editor_js(src, page) + """
const out = [];
for (const cid of [''].concat(CIDS)) {
    document.getElementById('client_id').value = String(cid);
    PRODUCTS.forEach(function (p) { out.push(['product', cid, p.id, productAdjustedPrice(p).toFixed(2)]); });
    DETAILS.forEach(function (d) { out.push(['detail', cid, d.id, detailAdjustedPrice(d.price).toFixed(2)]); });
}
console.log(JSON.stringify(out));
""".replace('CIDS', json.dumps(client_ids))
    bad = []
    with flask_app.app_context():
        clients = {c.id: c for c in Client.query.all()}
        for kind, cid, row_id, shown in _node(js):
            client = clients.get(cid) if cid != '' else None
            if kind == 'product':
                server = calculate_product_pricing(db.session.get(Product, row_id), client)['sell_price']
            else:
                server = detail_price_for(db.session.get(Detail, row_id), client)
            if shown != '%.2f' % server:
                bad.append((kind, cid, row_id, shown, '%.2f' % server))
    return bad


def random_carts(seed=7, n=300):
    rnd = random.Random(seed)
    carts = []
    for _ in range(n):
        lines = []
        for _ in range(rnd.randint(1, 8)):
            qty = rnd.choice([1, 2, 3, 7, 12, 250, 1000, 2.5, 0.75, 1.5, None])
            price = rnd.choice([round(rnd.uniform(0.01, 300), 2), round(rnd.uniform(0.01, 30), 3),
                                rnd.choice([0.125, 0.375, 2.625, 10.875]), None])
            lines.append({'quantity': qty, 'unit_price': price})
        carts.append({'lines': lines, 'discount': rnd.choice([0, 5, 7.5, 10, 12.345, 33.33, 2.5])})
    return carts


def totals_mismatches(src, carts):
    """offerTotals()/lineTotal() as the editor shows them vs Offer/OfferItem."""
    js = DOM_STUB + '\n'.join(_js_function(src, n) for n in ('round2', 'lineTotal', 'offerTotals')) + """
const CARTS = """ + json.dumps(carts) + """;
console.log(JSON.stringify(CARTS.map(function (c) {
    const t = offerTotals(c.lines, c.discount);
    return { lines: c.lines.map(function (l) { return lineTotal(l) == null ? null : lineTotal(l).toFixed(2); }),
             subtotal: t.subtotal.toFixed(2), discount: t.discount.toFixed(2), total: t.total.toFixed(2) };
})));
"""
    bad = []
    for cart, shown in zip(carts, _node(js)):
        offer = Offer(discount_percent=cart['discount'] or None)
        items = [OfferItem(quantity=l['quantity'], unit_price=l['unit_price']) for l in cart['lines']]
        offer.items = items
        server = {'lines': ['%.2f' % i.line_total if i.quantity is not None and i.unit_price is not None else None
                            for i in items],
                  'subtotal': '%.2f' % offer.subtotal, 'discount': '%.2f' % offer.discount_amount,
                  'total': '%.2f' % offer.total}
        if shown != server:
            bad.append((cart, shown, server))
    return bad


def product_offer_mismatches(browser, catalog, src):
    """offer.html's recomputeOfferPricing() vs calculate_product_pricing(product, client)."""
    product_ids, _detail_ids, client_ids = catalog
    bad = []
    # request outside an app context: a pushed one is reused by the request and loses the login
    pages = {pid: browser.get(f'/admin/products/{pid}/offer').get_data(as_text=True) for pid in product_ids}
    with flask_app.app_context():
        clients = {c.id: c for c in Client.query.all()}
        for pid, page in pages.items():
            consts = re.search(r'(const LIST_SELL_PRICE = .*?)\n\s*function', page, re.S).group(1)
            js = DOM_STUB + consts + '\n' + '\n'.join(
                _js_function(src, n) for n in ('applyClientAdjustment', 'round2', 'recomputeOfferPricing')
                if f'function {n}(' in src) + """
console.log(JSON.stringify(CIDS.map(function (cid) {
    recomputeOfferPricing(cid);
    return document.getElementById('offerSellPrice').textContent;
})));
""".replace('CIDS', json.dumps(client_ids))
            product = db.session.get(Product, pid)
            for cid, shown in zip(client_ids, _node(js)):
                server = '%.2f €' % calculate_product_pricing(product, clients[cid])['sell_price']
                if shown != server:
                    bad.append((pid, cid, shown, server))
    return bad


def _read(name):
    with open(os.path.join(TEMPLATES, name), encoding='utf-8') as f:
        return f.read()


@needs_node
def test_editor_prefill_matches_server(browser, catalog):
    bad = editor_mismatches(browser, catalog, _read('admin_offer_edit.html'))
    assert not bad, f'{len(bad)} mismatches, e.g. {bad[:5]}'


@needs_node
def test_editor_totals_match_print(catalog):
    bad = totals_mismatches(_read('admin_offer_edit.html'), random_carts())
    assert not bad, f'{len(bad)} mismatches, e.g. {bad[:3]}'


@needs_node
def test_product_offer_page_matches_server(browser, catalog):
    bad = product_offer_mismatches(browser, catalog, _read('offer.html'))
    assert not bad, f'{len(bad)} mismatches, e.g. {bad[:5]}'


@needs_node
def test_prefilled_price_is_what_create_order_freezes(browser, catalog):
    """A line saved with the editor's pre-filled price (not hand-edited) and
    turned into an order: quote, stored line and order line agree."""
    product_ids, detail_ids, client_ids = catalog
    src = _read('admin_offer_edit.html')
    page = browser.get('/admin/offers/new').get_data(as_text=True)
    pid, did, cid = product_ids[5], detail_ids[5], client_ids[0]
    prices = _node(editor_js(src, page) + f"""
document.getElementById('client_id').value = '{cid}';
const p = PRODUCTS.find(function (x) {{ return x.id === {pid}; }});
const d = DETAILS.find(function (x) {{ return x.id === {did}; }});
console.log(JSON.stringify([productAdjustedPrice(p).toFixed(2), detailAdjustedPrice(d.price).toFixed(2)]));
""")
    browser.post('/admin/offers/new', data={'client_id': str(cid), 'items_json': _items_json([
        {'type': 'product', 'name': 'QA P5', 'product_id': pid, 'quantity': 1, 'unit_price': prices[0]},
        {'type': 'detail', 'name': 'QA D5', 'detail_id': did, 'quantity': 1, 'unit_price': prices[1]},
    ])})
    with flask_app.app_context():
        offer = Offer.query.order_by(Offer.id.desc()).first()
        item_ids = [str(i.id) for i in offer.items]
        quoted = [i.unit_price for i in offer.items]
    browser.post(f'/admin/offers/{offer.id}/create-order', data={'customer_name': 'QA Prefill Order',
                                                                 'item_ids': item_ids})
    with flask_app.app_context():
        order = Order.query.filter_by(customer_name='QA Prefill Order').one()
        frozen = sorted(OrderItem.query.filter_by(order_id=order.id), key=lambda i: i.product_id is None)
        assert quoted == [float(x) for x in prices] == [i.unit_price for i in frozen]
