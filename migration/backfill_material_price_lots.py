"""
One-off: retroactively applies the price-lot split _find_or_create_delivery_
target() now does automatically for NEW material deliveries (see CLAUDE.md's
delivery-note "keep separate items separate" rule) to existing MaterialPrice
rows whose multi-price history predates that fix - every historical delivery
at a different price got silently pooled onto one row/price instead of
becoming its own selectable batch, which is what the production wizard
(admin_production_orders()) needs to offer a real choice between price lots.

For each MaterialPrice row, groups its DeliveryNoteItem history by price (a
line with no unit_price recorded is treated as the material's own current
cost_per_m2, i.e. "the default" lot, not a distinct one). Only splits when
every delivered unit can be accounted for - current stock_quantity exactly
equals the sum of every delivery on record, i.e. nothing has been issued/
consumed from this row yet (ClientDeliveryNote, ProductionOrder completion,
...). If some stock is already gone, there's no way to know which price lot
it came from, so that row is left untouched and reported rather than guessed
at. Historical DeliveryNoteItem rows are re-pointed at whichever split-off
row their price now belongs to, so admin_material_history() stays accurate
per lot.

Safe to re-run: a row with only one distinct price in its history (already
split, or never had more than one) is a no-op.

    python -m migration.backfill_material_price_lots
"""
from collections import defaultdict

from app import app, db, MaterialPrice, DeliveryNoteItem, _next_erp_number

with app.app_context():
    split_rows = 0
    new_lots = 0
    skipped_ambiguous = []

    for material in MaterialPrice.query.all():
        items = DeliveryNoteItem.query.filter_by(target_type='material', target_id=material.id).all()
        if not items:
            continue

        groups = defaultdict(list)  # price -> [DeliveryNoteItem, ...]
        for item in items:
            price = item.unit_price if item.unit_price is not None else material.cost_per_m2
            groups[price].append(item)

        if len(groups) <= 1:
            continue  # nothing to split - only one price ever recorded

        total_delivered = sum(item.quantity for group in groups.values() for item in group)
        if abs(total_delivered - (material.stock_quantity or 0)) > 0.01:
            skipped_ambiguous.append(material)
            continue

        # The group matching the row's own current price stays on it; every
        # other price group moves to a brand-new sibling row, same as a live
        # delivery-note split (_find_or_create_delivery_target).
        own_price = next((p for p in groups if abs(p - material.cost_per_m2) <= 0.001), None)
        own_items = groups.pop(own_price, [])
        material.stock_quantity = sum(item.quantity for item in own_items)

        for price, group_items in groups.items():
            new_row = MaterialPrice(
                key='pending', display_name=material.display_name, cost_per_m2=price,
                cutting_speed_mm_per_min=material.cutting_speed_mm_per_min,
                pierce_rate_per_min=material.pierce_rate_per_min,
                sheet_width_mm=material.sheet_width_mm, sheet_length_mm=material.sheet_length_mm,
                thickness_mm=material.thickness_mm, brand=material.brand, type=material.type,
                erp_number=_next_erp_number(), stock_quantity=sum(item.quantity for item in group_items),
            )
            db.session.add(new_row)
            db.session.flush()
            new_row.key = f'material_{new_row.id}'
            for item in group_items:
                item.target_id = new_row.id
            new_lots += 1

        split_rows += 1

    db.session.commit()

print(f"Split {split_rows} material row(s) into {new_lots} new price-lot row(s).")
if skipped_ambiguous:
    print(f"Skipped {len(skipped_ambiguous)} row(s) with multiple prices in their history but stock already "
          f"partially issued/consumed - can't tell which price lot that came from, left as one row:")
    for m in skipped_ambiguous:
        print(f"  - #{m.id} {m.display_name} ({m.brand or '-'}) - current stock {m.stock_quantity:g}")
