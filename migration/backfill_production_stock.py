"""
One-off: tops up Detail.stock_quantity to match production already recorded
before the admin_production_report() stock-bump went live (see app.py's
'/admin/production' POST handler - it only bumps stock on the *delta* of a
fresh update, so any quantity_produced entered before that code existed, or
before a given server restart picked it up, was never reflected in stock).

For each Detail, sums quantity_produced across every non-cancelled order's
standalone OrderItem lines and every product OrderItemComponent for that
Detail, and raises stock_quantity to that total if it's currently lower -
never lowers it, so stock that's already correct (or padded by a delivery
note on top) is left alone. Safe to run more than once.

    python -m migration.backfill_production_stock
"""
from app import app, db, Detail, Order, OrderItem, OrderItemComponent

with app.app_context():
    produced_totals = {}

    for item in OrderItem.query.filter(OrderItem.detail_id.isnot(None)).join(Order).filter(Order.status != 'cancelled').all():
        produced_totals[item.detail_id] = produced_totals.get(item.detail_id, 0) + item.quantity_produced

    for comp in OrderItemComponent.query.filter(OrderItemComponent.detail_id.isnot(None)).all():
        order = comp.order_item.order if comp.order_item else None
        if not order or order.status == 'cancelled':
            continue
        produced_totals[comp.detail_id] = produced_totals.get(comp.detail_id, 0) + comp.quantity_produced

    updated = 0
    for detail_id, total_produced in produced_totals.items():
        detail = db.session.get(Detail, detail_id)
        if not detail:
            continue
        current = detail.stock_quantity or 0
        if total_produced > current:
            print(f'  {detail.name}: {current:g} -> {total_produced:g}')
            detail.stock_quantity = total_produced
            updated += 1

    db.session.commit()

print(f"Topped up stock for {updated} detail(s) to match already-recorded production.")
