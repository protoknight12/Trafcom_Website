"""
One-time data step (run by migration/run_once.py), owner's decision 2026-11-19: material is sold with the standard
material markup (/admin/company "Стандартна надценка за материали", default 20%) unless the client has own Материали
terms. Detail.calculated_price is stored, so recompute it for every Detail priced from its material - public ones get
delivery cost x (1 + markup), owned ones the owner's terms or else the markup - with _reprice_material_details(), the
same function /admin/company runs when the markup changes. Skipped: manual-price details (no size) and details cut from
the owner's own lot (no material cost). Orders and offers keep their frozen prices.

Idempotent: a second run computes the same prices and changes nothing.

    python -m migration.data_apply_standard_material_markup
"""
from app import app, db, _reprice_material_details

with app.app_context():
    changed = _reprice_material_details()
    db.session.commit()

print(f'details repriced with the standard material markup: {changed}')
