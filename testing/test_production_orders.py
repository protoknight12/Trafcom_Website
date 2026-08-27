"""Self-check for _detail_material_unit_qty() - the per-piece material need
the production wizard (admin_production_orders()) multiplies by the chosen
quantity to size a job, and for ProductionOrder's display-unit conversion
(mm for bar stock, m² for sheets - see planned_display/actual_display).
Mirrors _material_cost()'s type branching, so both must be kept in sync.
Also covers complete_production_order()/delete_production_order()'s shared
_bump_stock() arithmetic: completing a job then deleting it must land stock
back exactly where it started, regardless of how much the actual material
used differed from the plan (waste/kerf).

    python -m testing.test_production_orders
"""
from types import SimpleNamespace

from app import _detail_material_unit_qty, _material_available_qty, _material_stock_delta, _bump_stock, ProductionOrder

sheet_detail = SimpleNamespace(effective_width=1000.0, effective_height=500.0,
                                material=SimpleNamespace(type='sheets'))
assert _detail_material_unit_qty(sheet_detail) == 0.5, "1000mm x 500mm sheet piece = 0.5 m^2"

rod_detail = SimpleNamespace(effective_width=20.0, effective_height=100.0,
                              material=SimpleNamespace(type='rods'))
assert _detail_material_unit_qty(rod_detail) == 0.1, "100mm of rod stock = 0.1 m, width/diameter is irrelevant"

# 10 pieces at 100mm each planned, matches the "1000mm planned" example from
# the feature request - stored internally in meters (0.1 m/piece x 10 = 1.0 m).
job = ProductionOrder(quantity=10, planned_material_qty=1.0, actual_material_qty=1.1,
                       material=SimpleNamespace(type='rods'))
assert job.planned_display == 1000.0, "planned 1.0m shown to the user as 1000mm"
assert job.actual_display == 1100.0, "actual 1.1m (used more than planned) shown as 1100mm"
assert job.unit_label == 'мм'

sheet_job = ProductionOrder(quantity=4, planned_material_qty=2.0, material=SimpleNamespace(type='sheets'))
assert sheet_job.planned_display == 2.0, "sheet stock stays in m^2, not converted to mm"
assert sheet_job.actual_display is None, "no actual_material_qty entered yet (job still pending)"
assert sheet_job.unit_label == 'м²'

# complete then delete a job whose actual usage (1.1m) overshot the plan
# (1.0m, e.g. 10 pcs x 0.1m) - deleting a 'done' job must undo exactly what
# completing it did, landing both stocks back at their starting values no
# matter the waste, per complete_production_order()/delete_production_order().
material = SimpleNamespace(stock_quantity=5.0, type='rods')
detail = SimpleNamespace(stock_quantity=20.0)
quantity, actual_used = 10, 1.1

_bump_stock(material, -actual_used)   # complete_production_order()
_bump_stock(detail, quantity)
assert material.stock_quantity == 5.0 - 1.1
assert detail.stock_quantity == 20.0 + 10

_bump_stock(material, actual_used)    # delete_production_order() on a 'done' job
_bump_stock(detail, -quantity)
assert material.stock_quantity == 5.0, "material stock must return to exactly its starting value"
assert detail.stock_quantity == 20.0, "detail stock must return to exactly its starting value"

# _material_available_qty()/_material_stock_delta(): sheet stock is a SHEET
# COUNT (stock_quantity), not a running m² total - available area is
# stock_quantity x one sheet's own area (sheet_width_mm x sheet_length_mm,
# the catalog dims, never the cut part's own size).
sheet_material = SimpleNamespace(stock_quantity=3.0, type='sheets', sheet_width_mm=1000.0, sheet_length_mm=2000.0)
assert _material_available_qty(sheet_material) == 6.0, "3 sheets x 2 m^2/sheet = 6 m^2 available"
assert _material_stock_delta(sheet_material, 1.0) == 0.5, "using 1 m^2 consumes half of one 2 m^2 sheet"

# Rods/pipes/profiles are unchanged (still a running length total, never a
# bar count) regardless of whether sheet_width_mm/sheet_length_mm happen to
# be set (they store diameter/rod-length for that type, not "one sheet").
rod_material = SimpleNamespace(stock_quantity=5.0, type='rods', sheet_width_mm=14.0, sheet_length_mm=3000.0)
assert _material_available_qty(rod_material) == 5.0
assert _material_stock_delta(rod_material, 0.05) == 0.05

# Legacy/no-sheet-size-on-record materials fall back to treating
# stock_quantity as already the native unit (m²) - dividing by an unknown
# sheet size would be meaningless, not merely imprecise.
no_dims_material = SimpleNamespace(stock_quantity=2.5, type='sheets', sheet_width_mm=None, sheet_length_mm=None)
assert _material_available_qty(no_dims_material) == 2.5
assert _material_stock_delta(no_dims_material, 1.0) == 1.0

# Full round trip through the real sheet-count conversion: create (compare
# against available area) -> complete (convert actual m² used into a sheet-
# count delta) -> delete (convert back) must land stock_quantity exactly
# where it started, same guarantee as the rods case above.
sheet_material2 = SimpleNamespace(stock_quantity=3.0, type='sheets', sheet_width_mm=1000.0, sheet_length_mm=2000.0)
actual_m2_used = 1.3
_bump_stock(sheet_material2, -_material_stock_delta(sheet_material2, actual_m2_used))  # complete
assert round(sheet_material2.stock_quantity, 10) == 3.0 - (1.3 / 2.0)
_bump_stock(sheet_material2, _material_stock_delta(sheet_material2, actual_m2_used))   # delete (undo)
assert round(sheet_material2.stock_quantity, 10) == 3.0, "sheet-count stock must return to exactly its starting value"

print("ok")
