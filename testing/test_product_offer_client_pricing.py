"""
pytest regression test: the printable single-Product offer document
(admin_product_offer(), templates/offer.html - the older Ctrl+P/PDF flow,
distinct from the Offer/OfferItem model's admin_offer_edit.html) lets an
admin pick a Client from a dropdown to fill in the customer name, but never
actually adjusted the price table for that client's discount/markup - the
dropdown was purely cosmetic for the displayed name. Guards that the page
now also exposes each detail's base (undiscounted) unit price and every
Client's Детайли terms so offer.html's JS (recomputeOfferPricing()) can
apply the picked client's discount/markup to the whole price table, same as
order_create.html/admin_offer_edit.html already do.

protocol.html/certificate.html carry no pricing at all, so they need no
equivalent coverage.

Run with:
    pytest testing/test_product_offer_client_pricing.py -v
"""
import atexit
import os
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

from app import app as flask_app, db, User, Client, MaterialPrice, Detail, Product, ProductDetail, limiter


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        client = Client(name='QA Discount Client', detail_adjustment_type='discount', detail_adjustment_percent=15)
        db.session.add_all([admin, client])
        material = MaterialPrice(key='qa_mat', display_name='QA Mat', type='sheets', cost_per_m2=10.0,
                                  cutting_speed_mm_per_min=1000, pierce_rate_per_min=30)
        db.session.add(material)
        db.session.flush()
        detail = Detail(name='QA Detail', material_key=material.key, width=100, height=100,
                         total_length=0, pierce_count=0, calculated_price=20.0)
        db.session.add(detail)
        db.session.commit()
        product = Product(name='QA Product', markup_percent=10)
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
        db.session.commit()

        yield flask_app, product.id, client.id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def admin_browser(app):
    flask_app, product_id, client_id = app
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c, product_id, client_id


def test_offer_page_exposes_base_detail_price_and_client_terms(admin_browser):
    c, product_id, client_id = admin_browser
    res = c.get(f'/admin/products/{product_id}/offer')
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # Base (undiscounted) unit price - the JS applies the discount at pick time.
    assert 'unitPrice: 20.0' in body
    assert f'"{client_id}"' in body
    assert '"detail_type": "discount"' in body or '"detail_type":"discount"' in body
    assert '"detail_percent": 15' in body or '"detail_percent":15' in body
    # The recompute cell ids the JS writes into must exist.
    for cell_id in ('pd-unit-0', 'pd-total-0', 'offerDetailsSubtotal', 'offerTotalCost',
                    'offerMarkupAmount', 'offerSellPrice'):
        assert f'id="{cell_id}"' in body
