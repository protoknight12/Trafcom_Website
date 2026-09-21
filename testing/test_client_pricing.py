"""Self-check for per-client discount/markup pricing (_apply_adjustment(),
_client_service_adjustment(), and how they're wired into _material_cost(),
calculate_cnc_price_multi_service(), calculate_product_pricing() and
detail_price_for()) - see CLAUDE.md's "Клиентско ценообразуване" plan.

    python -m testing.test_client_pricing
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

from app import (app, db, _apply_adjustment, _client_service_adjustment, _material_cost,
                  calculate_cnc_price_multi_service, calculate_product_pricing, detail_price_for,
                  Client, ClientServicePrice, Service, MaterialPrice, Detail, Product, ProductDetail)

with app.app_context():
    db.create_all()

    # --- _apply_adjustment: discount/markup/none branch coverage ---
    assert _apply_adjustment(100.0, 'discount', 10) == 90.0
    assert round(_apply_adjustment(100.0, 'markup', 10), 2) == 110.0
    assert _apply_adjustment(100.0, None, 10) == 100.0, "no type -> unchanged"
    assert _apply_adjustment(100.0, 'discount', None) == 100.0, "no percent -> unchanged"
    assert _apply_adjustment(100.0, 'discount', 0) == 100.0, "0% -> unchanged"

    material = MaterialPrice(key='qa_sheet', display_name='QA Ламарина', type='sheets', cost_per_m2=10.0,
                              cutting_speed_mm_per_min=1000, pierce_rate_per_min=30)
    service = Service(name='QA Лазер', price_per_hour_eur=60.0)
    db.session.add_all([material, service])
    db.session.flush()

    client_discount = Client(name='QA Отстъпков клиент', material_adjustment_type='discount',
                              material_adjustment_percent=10, detail_adjustment_type='discount',
                              detail_adjustment_percent=5)
    client_plain = Client(name='QA Клиент без условия')
    db.session.add_all([client_discount, client_plain])
    db.session.flush()
    db.session.add(ClientServicePrice(client_id=client_discount.id, service_id=service.id,
                                       adjustment_type='discount', adjustment_percent=20))
    db.session.commit()

    # --- _material_cost: client's Материали discount applies, unlinked/no-client doesn't ---
    base_cost = _material_cost(1000, 500, material)  # 0.5 m^2 * 10 = 5.0
    assert base_cost == 5.0
    assert _material_cost(1000, 500, material, client_discount) == 4.5, "10% discount on material cost"
    assert _material_cost(1000, 500, material, client_plain) == 5.0, "client with no terms set -> unchanged"
    assert _material_cost(1000, 500, material, None) == 5.0

    # --- _client_service_adjustment: override row vs. no row ---
    assert _client_service_adjustment(client_discount, service) == ('discount', 20)
    assert _client_service_adjustment(client_plain, service) == (None, 0), "no ClientServicePrice row -> no-op"
    assert _client_service_adjustment(None, service) == (None, 0)

    # --- calculate_cnc_price_multi_service: material + service + overall Детайли layer stack ---
    price_no_client = calculate_cnc_price_multi_service(1000, 500, 2000, 2, 'qa_sheet', [service.id])
    price_with_client = calculate_cnc_price_multi_service(1000, 500, 2000, 2, 'qa_sheet', [service.id], client_discount)
    assert price_with_client < price_no_client, "a client with discounts on every layer must always price lower"

    # --- calculate_product_pricing: Детайли discount hits only the details subtotal ---
    detail = Detail(name='QA Detail', material_key=material.key, width=1000.0, height=500.0,
                     total_length=0.0, pierce_count=0, calculated_price=5.0)
    db.session.add(detail)
    db.session.flush()
    product = Product(name='QA Product', markup_percent=50.0)
    db.session.add(product)
    db.session.flush()
    db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
    db.session.commit()

    pricing_plain = calculate_product_pricing(product)
    pricing_discounted = calculate_product_pricing(product, client_discount)
    # details_subtotal = 2 x 5.0 = 10.0; with 5% Детайли discount -> 9.5;
    # markup 50% on top -> 14.25 (vs 15.0 undiscounted). Markup itself is untouched.
    assert pricing_plain['details_subtotal'] == 10.0
    assert pricing_plain['sell_price'] == 15.0
    assert pricing_discounted['details_subtotal'] == 9.5
    assert pricing_discounted['sell_price'] == 14.25

    # --- detail_price_for: standalone Detail catalog price ---
    assert detail_price_for(detail, None) == detail.total_price
    assert detail_price_for(detail, client_discount) == round(detail.total_price * 0.95, 2)

    db.session.remove()
    db.drop_all()

print("ok")
