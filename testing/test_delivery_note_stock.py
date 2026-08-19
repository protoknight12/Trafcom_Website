"""Self-check for _bump_stock(), the core arithmetic behind delivery-note
intake (see create_delivery_note() in app.py) - a delivery note line item
should add its quantity onto the target's stock_quantity, starting from
None (fresh row, never restocked before) or an existing value. Also covers
the reverse flow (create_client_delivery_note()), which calls the same
function with a negative quantity to issue stock to a client.

    python -m testing.test_delivery_note_stock
"""
from types import SimpleNamespace

from app import _bump_stock

fresh = SimpleNamespace(stock_quantity=None)
assert _bump_stock(fresh, 5) == 5, "first delivery onto a never-restocked row should start from 0"
assert fresh.stock_quantity == 5

existing = SimpleNamespace(stock_quantity=10.5)
assert _bump_stock(existing, 2.5) == 13.0, "should add onto the existing stock, not replace it"

issued = SimpleNamespace(stock_quantity=8)
assert _bump_stock(issued, -3) == 5, "a client delivery note issues stock via a negative quantity"
assert issued.stock_quantity == 5

print("ok")
