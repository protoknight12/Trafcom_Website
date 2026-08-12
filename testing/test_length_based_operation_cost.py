"""Self-check for Service.pricing_mode / Operation.cost's length-based branch
(a length-priced laser cutting operation, billed by cut length in mm rather
than duration_minutes - see app.py) and _add_operations_from_rows()'s row
validation for both pricing modes.

    python -m testing.test_length_based_operation_cost
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from app import Operation, Service, _add_operations_from_rows, db, app


def _op(service, **kwargs):
    defaults = dict(duration_minutes=0.0, length_mm=None)
    defaults.update(kwargs)
    o = Operation(**defaults)
    o.service = service
    return o


time_service = Service(name='Фрезоване', price_per_hour_eur=60.0, pricing_mode='time')
length_service = Service(name='Лазер по дължина', price_per_hour_eur=1.0, pricing_mode='length', price_per_meter_eur=5.0)

assert _op(time_service, duration_minutes=10).cost == 10.0  # 10 min @ 1 EUR/min
assert _op(length_service, length_mm=2000).cost == 10.0  # 2 m @ 5 EUR/m
assert _op(length_service, length_mm=None).cost == 0.0
assert _op(length_service, duration_minutes=999, length_mm=1000).cost == 5.0  # ignores duration_minutes

# _add_operations_from_rows: time-mode row needs duration_minutes, length-mode needs length_mm
with app.app_context():
    db.create_all()
    from app import MaterialPrice, Detail
    material = MaterialPrice(key='qa_len_mat', display_name='QA', cost_per_m2=1,
                              cutting_speed_mm_per_min=1, pierce_rate_per_min=1)
    db.session.add_all([material, time_service, length_service])
    db.session.flush()
    detail = Detail(name='QA Detail', material_key=material.key, width=1, height=1,
                     total_length=1, pierce_count=1, calculated_price=1.0)
    db.session.add(detail)
    db.session.flush()

    added = _add_operations_from_rows(detail, [
        {'service_id': time_service.id, 'duration_minutes': 5},          # valid time row
        {'service_id': length_service.id, 'length_mm': 1500},            # valid length row
        {'service_id': length_service.id, 'duration_minutes': 5},        # length service, no length_mm -> skipped
        {'service_id': time_service.id, 'length_mm': 1500},              # time service, no duration -> skipped
        {'service_id': length_service.id, 'length_mm': -5},              # non-positive -> skipped
    ])
    assert added == 2, added
    ops = Operation.query.filter_by(detail_id=detail.id).order_by(Operation.sequence).all()
    assert ops[0].duration_minutes == 5.0 and ops[0].length_mm is None
    assert ops[1].length_mm == 1500.0
    db.session.rollback()
    db.drop_all()

print("ok")
