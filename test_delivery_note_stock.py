"""Self-check for _bump_stock(), the core arithmetic behind delivery-note
intake (see create_delivery_note() in app.py) - a delivery note line item
should add its quantity onto the target's stock_quantity, starting from
None (fresh row, never restocked before) or an existing value.

    python test_delivery_note_stock.py
"""
from types import SimpleNamespace

from app import _bump_stock

fresh = SimpleNamespace(stock_quantity=None)
assert _bump_stock(fresh, 5) == 5, "first delivery onto a never-restocked row should start from 0"
assert fresh.stock_quantity == 5

existing = SimpleNamespace(stock_quantity=10.5)
assert _bump_stock(existing, 2.5) == 13.0, "should add onto the existing stock, not replace it"

print("ok")
