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

from app import _detail_material_unit_qty, _bump_stock, ProductionOrder

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

print("ok")
