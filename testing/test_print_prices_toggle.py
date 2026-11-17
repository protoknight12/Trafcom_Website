"""
pytest: every browser-print document that shows prices has the "Показвай цените" toggle
(static/js/print_prices.js + `body.no-prices .price {display:none}`), every price-like text
(NN.NN, EUR, €) sits inside an element marked class="price", and documents with no prices
(supplier-less client receipt, client note without prices) carry no toggle.

Run with:
    pytest testing/test_print_prices_toggle.py -v
"""
import atexit
import os
import re
import tempfile
from datetime import date
from html.parser import HTMLParser

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

from app import (app as flask_app, db, User, Client, Supplier, MaterialPrice, Detail, Product, ProductDetail,
                 ProductExtraCost, Offer, OfferItem, DeliveryNote, DeliveryNoteItem, ClientDeliveryNote,
                 ClientDeliveryNoteItem, limiter)

PRICE_RE = re.compile(r'(?<![\d.])\d+\.\d{2}(?![\d.])|EUR|€')
VOID = {'br', 'img', 'input', 'meta', 'link', 'hr'}


class _Scan(HTMLParser):
    """Collects text that looks like a price but has no class="price" ancestor."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.unmarked = []
        self.in_sheet = False
        self.price_texts = 0

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        cls = (dict(attrs).get('class') or '').split()
        self.stack.append((tag, 'price' in cls, 'doc-sheet' in cls))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if any(t in ('script', 'style', 'title') for t, _, _ in self.stack):
            return
        if not any(sheet for _, _, sheet in self.stack):  # toolbar/navbar are not the document
            return
        if PRICE_RE.search(data):
            if any(p for _, p, _ in self.stack):
                self.price_texts += 1
            else:
                self.unmarked.append(data.strip())


def _check(html, toggle=True):
    assert ('data-print-prices' in html) == toggle
    if not toggle:
        return
    assert 'Показвай цените' in html
    assert 'body.no-prices .price' in html
    assert 'js/print_prices.js' in html
    scan = _Scan()
    scan.feed(html)
    assert scan.price_texts >= 2  # the document really has price cells
    assert scan.unmarked == [], scan.unmarked


@pytest.fixture
def ctx():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        client = Client(name='QA Client')
        supplier = Supplier(name='QA Supplier')
        db.session.add_all([client, supplier])
        mat = MaterialPrice(key='qa_mat', display_name='QA Mat', type='sheets', cost_per_m2=10.0,
                            cutting_speed_mm_per_min=1000, pierce_rate_per_min=30)
        db.session.add(mat)
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=mat.key, width=100, height=100,
                        total_length=0, pierce_count=0, calculated_price=20.0)
        db.session.add(detail)
        db.session.flush()
        product = Product(name='QA Product', markup_percent=10)
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        db.session.add(ProductExtraCost(product_id=product.id, label='Боя', amount=5.5))

        offer = Offer(number='00000000999', object_title='Обект', discount_percent=10, valid_until=date(2030, 1, 1),
                      client_id=client.id)
        db.session.add(offer)
        db.session.flush()
        db.session.add(OfferItem(offer_id=offer.id, position=0, item_type='product', code='1', name='Ред',
                                 quantity=3, unit='бр', unit_price=12.5))
        db.session.add(OfferItem(offer_id=offer.id, position=1, item_type='text', description_html='бележка'))

        def dn(**kw):
            n = DeliveryNote(**kw)
            db.session.add(n)
            db.session.flush()
            db.session.add(DeliveryNoteItem(delivery_note_id=n.id, target_type='material', target_id=mat.id,
                                            description_snapshot='QA Mat', quantity=3, unit_price=12.5))
            return n

        sup_note = dn(supplier_id=supplier.id)
        recv_note = dn(client_id=client.id)

        def cdn(kind, price):
            n = ClientDeliveryNote(client_id=client.id, kind=kind)
            db.session.add(n)
            db.session.flush()
            db.session.add(ClientDeliveryNoteItem(client_delivery_note_id=n.id, target_type='material',
                                                  target_id=mat.id, description_snapshot='QA Mat', quantity=3,
                                                  unit_price=price))
            return n

        ret_note, con_note, free_note = cdn('return', 12.5), cdn('consume', 7.25), cdn('return', None)
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c, dict(offer=offer.id, product=product.id, sup=sup_note.id, recv=recv_note.id,
                      ret=ret_note.id, con=con_note.id, free=free_note.id)
        db.session.remove()
        db.drop_all()


def _get(c, url):
    r = c.get(url)
    assert r.status_code == 200
    return r.data.decode('utf-8')


def test_admin_offer_print(ctx):
    c, ids = ctx
    html = _get(c, f"/admin/offers/{ids['offer']}/print")
    _check(html)
    assert 'Отстъпка' in html  # the discount rows exist and are marked (checked by _check)


def test_product_offer(ctx):
    c, ids = ctx
    _check(_get(c, f"/admin/products/{ids['product']}/offer"))


def test_supplier_delivery_note(ctx):
    c, ids = ctx
    _check(_get(c, f"/admin/delivery-notes/{ids['sup']}/print"))


def test_client_receipt_has_no_prices_and_no_toggle(ctx):
    c, ids = ctx
    html = _get(c, f"/admin/delivery-notes/{ids['recv']}/print")
    _check(html, toggle=False)
    assert '12.50' not in html


@pytest.mark.parametrize('key', ['ret', 'con'])
def test_client_delivery_note(ctx, key):
    c, ids = ctx
    _check(_get(c, f"/admin/client-delivery-notes/{ids[key]}/print"))


def test_client_delivery_note_without_prices_has_no_toggle(ctx):
    c, ids = ctx
    _check(_get(c, f"/admin/client-delivery-notes/{ids['free']}/print"), toggle=False)


def test_vat_rows_under_the_total(ctx):
    """Priced documents end with "Сума без ДДС", "ДДС 20%" and "Общо с ДДС" (3 x 12.50 = 37.50)."""
    c, ids = ctx
    for url in (f"/admin/delivery-notes/{ids['sup']}/print", f"/admin/client-delivery-notes/{ids['ret']}/print"):
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', _get(c, url)))
        assert 'Сума без ДДС 37.50 EUR' in text and 'ДДС 20% 7.50 EUR' in text and 'Общо с ДДС 45.00 EUR' in text
    assert 'Общо с ДДС' in _get(c, f"/admin/offers/{ids['offer']}/print")
    assert 'Общо с ДДС' not in _get(c, f"/admin/client-delivery-notes/{ids['free']}/print")   # no prices, no VAT
