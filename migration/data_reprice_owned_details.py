"""
One-time data step (run by migration/run_once.py): Details that were already private to a client before the
owner-pricing rule (commit 8520859) still hold the plain delivery price. Recompute calculated_price for every
owned Detail with geometry, the same way admin_update_detail_owner() does now (_detail_material_price()).
Public details and manual-price details (no width/height) are left alone.

    python -m migration.data_reprice_owned_details
"""
from app import app, db, Detail, _detail_material_price

with app.app_context():
    changed = 0
    for detail in Detail.query.filter(Detail.owner_client_id.isnot(None)).all():
        if not (detail.width and detail.height and detail.material):
            continue
        new_price = _detail_material_price(detail)
        if new_price != detail.calculated_price:
            detail.calculated_price = new_price
            changed += 1
    db.session.commit()

print(f'owned details repriced: {changed}')
