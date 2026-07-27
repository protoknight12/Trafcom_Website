"""
One-off: assigns an auto-generated unique ERP № (see _next_erp_number() in
app.py) to any existing Detail/Product/MaterialPrice row that doesn't have
one yet - covers rows created before ERP № became auto-generated,
including the seed_test_data.py fixtures. Safe to run more than once
(only touches rows where erp_number IS NULL).

    "D:/python_3131_interpreter/Scripts/python.exe" backfill_erp_numbers.py
"""
from app import app, db, Detail, Product, MaterialPrice, _next_erp_number

with app.app_context():
    count = 0
    for model in (Detail, Product, MaterialPrice):
        for row in model.query.filter(model.erp_number.is_(None)).all():
            row.erp_number = _next_erp_number()
            db.session.flush()  # so the next _next_erp_number() call sees this one too
            count += 1
    db.session.commit()

print(f"Assigned ERP № to {count} row(s) that didn't have one.")
