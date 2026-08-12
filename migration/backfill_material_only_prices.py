"""
One-off backfill: recomputes calculated_price for every existing Detail that
came from an actual DXF (geometry_json is set) to the new material-only
formula (calculate_material_price() - see the Detail-catalog pricing
refactor in app.py). Before this, calculated_price still held the
pre-refactor combined material+cutting(+setup fee) value from whenever the
detail was created/last material-updated - db.create_all() never recomputes
existing rows on its own, and neither did the code change by itself.

Bare-bones/manual-price details (geometry_json is None, no DXF) are left
untouched - they were never priced by calculate_cnc_price() in the first
place, so there's nothing stale to recompute; recomputing them would zero
out a manually-entered price instead.

Does NOT retroactively create a cutting Operation for the cutting cost that
gets dropped out of calculated_price - there's no reliable historical record
of which service originally cut each of these. Detail.total_price will drop
by that amount for every affected row; add a cutting Operation by hand
afterward (detail_dxf_dashboard.html's Операции tab) for any detail where
that cost should still be charged.

Safe to run more than once (recomputing is idempotent).

    python -m migration.backfill_material_only_prices
"""
from app import app, db, Detail, calculate_material_price

with app.app_context():
    count = 0
    for detail in Detail.query.filter(Detail.geometry_json.isnot(None)).all():
        new_price = calculate_material_price(detail.width, detail.height, detail.material_key)
        if new_price != detail.calculated_price:
            detail.calculated_price = new_price
            count += 1
    db.session.commit()

print(f"Recomputed calculated_price (material-only) for {count} detail(s).")
