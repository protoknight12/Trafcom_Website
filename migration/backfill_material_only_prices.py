"""
One-off backfill: recomputes calculated_price for every existing Detail that
has real dimensions (width > 0 or height > 0 - whether or not it came from an
actual DXF, geometry_json set or not: some imported/legacy catalog rows carry
width/height with no DXF ever attached, e.g. ported-in ERP data) to the
current material-only formula (calculate_material_price() - see the
Detail-catalog pricing refactor in app.py, and the pipes-vs-rods fix in
_material_cost()). Before this, calculated_price still held whatever the
formula produced at creation/last-material-update time - db.create_all()
never recomputes existing rows on its own, and neither does a code change by
itself. Re-run this after importing/porting in Detail data from elsewhere
(e.g. production) so newly-arrived rows get today's formula too, not just
rows that existed the last time this ran.

Bare-bones/manual-price details (width == 0 and height == 0, no dimensions
at all) are left untouched - they were never priced by a formula in the
first place, so there's nothing stale to recompute; recomputing them would
zero out a manually-entered price instead.

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
    for detail in Detail.query.filter((Detail.width > 0) | (Detail.height > 0)).all():
        new_price = calculate_material_price(detail.width, detail.height, detail.material_key)
        if new_price != detail.calculated_price:
            detail.calculated_price = new_price
            count += 1
    db.session.commit()

print(f"Recomputed calculated_price (material-only) for {count} detail(s).")
