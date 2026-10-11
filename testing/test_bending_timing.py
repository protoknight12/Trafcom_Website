"""bending/timing.estimate(): hand-computed examples. Run: python -m testing.test_bending_timing"""
from bending.timing import estimate

e = estimate(4, 1, 0)   # 4*25 s + 1*10 s + 20 s load = 130 s
assert e['per_part_min'] == round(130 / 60, 2) and e['change_min'] == 0 and e['batch_setup_min'] == 10.0
e = estimate(2, 0, 1, {'tool_change_min': 5, 'bend_s': 30})   # 60 s + 20 s + 5 min
assert e['per_part_min'] == round(80 / 60 + 5, 2)
assert estimate(0, 0, 0) == {'bend_min': 0.0, 'flip_min': 0.0, 'load_min': 0.0, 'change_min': 0.0, 'per_part_min': 0.0, 'batch_setup_min': 0.0}
print('ok')
