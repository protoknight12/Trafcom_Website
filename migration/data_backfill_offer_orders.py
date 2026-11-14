"""
One-time data step (run by migration/run_once.py): offers turned into orders before offer.order_id existed are linked
from the ActivityLog line admin_offer_create_order() wrote ("Създадена поръчка <order_number> от оферта № <number> ...").
Only offers with no order yet are touched, and only when both the offer (by number) and the order (by order_number)
still exist; the newest log line wins. Safe to run more than once.

    python -m migration.data_backfill_offer_orders
"""
import re

from app import app, db, ActivityLog, Offer, Order

PATTERN = re.compile(r'Създадена поръчка (\S+) от оферта № (\S+)')

with app.app_context():
    offers = {o.number: o for o in Offer.query.filter(Offer.order_id.is_(None)).all()}
    orders = {o.order_number: o for o in Order.query.all()}
    linked = 0
    for row in ActivityLog.query.filter(ActivityLog.action.like('Създадена поръчка%')).order_by(ActivityLog.id.desc()).all():
        m = PATTERN.match(row.action)
        offer = m and offers.get(m.group(2))
        order = m and orders.get(m.group(1))
        if offer and order:
            offer.order_id = order.id
            del offers[offer.number]  # newest line only
            linked += 1
    db.session.commit()

print(f'offers linked to their order: {linked}')
