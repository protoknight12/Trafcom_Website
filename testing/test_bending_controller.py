"""bending.controller against the numbers stored in the real AD-ES 2040 products (prd.123: P.97.75.R08 / M460.R85-16, 1 mm steel). Run: python -m testing.test_bending_controller"""
from bending.controller import r_default, y_positions

TOOL_REF, PUNCH_H, DIE_H = 338.0, 97.59558, 80.0      # machine parameter, pun.P.97.75.R08.dld ToolHeight, die.M460.R85-16.dld ToolHeight
y = y_positions(TOOL_REF, PUNCH_H, DIE_H, thickness=1.0, depth=2.226667)
assert abs(y['mute'] - 155.40442) < 1e-3                  # = the MutePosition stored in prd.123
assert abs(y['zero'] - 160.40442) < 1e-3 and abs(y['bottom'] - 162.631087) < 1e-3 and y['contact'] > y['mute']
assert abs(r_default(DIE_H) - 80.00095) < 1e-3            # the R of the flat first bends in prd.123 / prd.999
assert abs(r_default(120.0) - 120.0009) < 1e-3            # ... and of the 120 mm dies of the demo products
assert r_default(DIE_H, 'hang') < r_default(DIE_H)        # a hanging flange: the finger is lowered
print('ok')
