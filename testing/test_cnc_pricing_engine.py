"""Self-check for the time-based pricing engine (calculate_cnc_price()) and
Detail.total_price - see the CLAUDE.md "DXF geometry -> price pipeline" /
"Detail Operations" tasks. Uses a throwaway file-based SQLite DB, same trick
as test_delivery_note_matching.py, since both need a real app/db context.

    python -m testing.test_cnc_pricing_engine
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

from app import app, db, MaterialPrice, Service, Detail, Operation, calculate_cnc_price, calculate_cnc_price_multi_service

with app.app_context():
    db.create_all()

    material = MaterialPrice(key='steel_test', display_name='Test Steel', cost_per_m2=20.0,
                              cutting_speed_mm_per_min=1.2, pierce_rate_per_min=10.0)
    service = Service(name='Test Laser', price_per_hour_eur=50.0)
    db.session.add_all([material, service])
    db.session.commit()

    # -- Worked example from the spec: 50 EUR/h, 1.2 mm/min cutting speed,
    #    200mm cut, 5 pierces at 10 pierces/min, 100x50mm part -------------
    price = calculate_cnc_price(width=100, height=50, total_length=200, pierce_count=5,
                                 material_key=material.key, service_id=service.id)
    area_m2 = (100 * 50) / 1_000_000
    material_cost = area_m2 * 20.0
    cutting_time_min = 200 / 1.2
    pierce_time_min = 5 / 10.0
    time_cost = (cutting_time_min + pierce_time_min) * (50.0 / 60.0)
    expected = round(material_cost + time_cost + 5.00, 2)  # BASE_SETUP_FEE = 5.00
    assert abs(price - expected) < 0.01, f"expected {expected}, got {price}"
    assert abs(price - 144.41) < 0.01, f"expected ~144.41 for the worked example, got {price}"

    # -- Invalid material/service must fall back to 0.0, not raise ----------
    assert calculate_cnc_price(100, 50, 200, 5, 'no-such-material', service.id) == 0.0
    assert calculate_cnc_price(100, 50, 200, 5, material.key, 999999) == 0.0
    assert calculate_cnc_price(100, 50, 200, 5, material.key, None) == 0.0

    # -- Detail.total_price = calculated_price + every attached Operation's
    #    cost (duration_minutes priced at that op's own Service rate) -------
    fast_service = Service(name='Mill A', price_per_hour_eur=60.0)
    slow_service = Service(name='Mill B', price_per_hour_eur=40.0)
    db.session.add_all([fast_service, slow_service])
    db.session.commit()

    detail = Detail(name='Test Detail', material_key=material.key, width=100, height=50,
                     total_length=200, pierce_count=5, calculated_price=50.0, cutting_service_id=service.id)
    db.session.add(detail)
    db.session.commit()

    db.session.add_all([
        Operation(detail_id=detail.id, service_id=fast_service.id, sequence=0, duration_minutes=10.0),  # 10 min @ 60/h = 10.00
        Operation(detail_id=detail.id, service_id=slow_service.id, sequence=1, duration_minutes=30.0),  # 30 min @ 40/h = 20.00
    ])
    db.session.commit()

    assert abs(detail.total_price - 80.0) < 0.01, f"expected 50 + 10 + 20 = 80.0, got {detail.total_price}"

    # A detail with no operations just falls back to its base cut price.
    bare_detail = Detail(name='No Ops', material_key=material.key, width=10, height=10,
                          total_length=10, pierce_count=1, calculated_price=12.34)
    db.session.add(bare_detail)
    db.session.commit()
    assert bare_detail.total_price == 12.34

    # -- calculate_cnc_price_multi_service (DXF calculator's checkbox picker):
    #    material cost + BASE_SETUP_FEE charged once, but cutting+pierce time
    #    priced at EVERY selected service's rate and summed -------------------
    multi_price = calculate_cnc_price_multi_service(
        width=100, height=50, total_length=200, pierce_count=5,
        material_key=material.key, service_ids=[service.id, fast_service.id]
    )
    time_cost_service = (cutting_time_min + pierce_time_min) * (50.0 / 60.0)
    time_cost_fast = (cutting_time_min + pierce_time_min) * (60.0 / 60.0)
    expected_multi = round(material_cost + time_cost_service + time_cost_fast + 5.00, 2)
    assert abs(multi_price - expected_multi) < 0.01, f"expected {expected_multi}, got {multi_price}"
    # A single service in the list must match the single-service function exactly.
    assert calculate_cnc_price_multi_service(100, 50, 200, 5, material.key, [service.id]) == price
    # No services / unknown material must fall back to 0.0, not raise.
    assert calculate_cnc_price_multi_service(100, 50, 200, 5, material.key, []) == 0.0
    assert calculate_cnc_price_multi_service(100, 50, 200, 5, 'no-such-material', [service.id]) == 0.0

print("ok")
