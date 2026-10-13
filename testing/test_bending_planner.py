"""bending.planner on the real P.97.75.r08 punch and M460.r die. Run: python -m testing.test_bending_planner"""
import migration.seed_bend_tools_m460r as m
from bending.planner import plan
from bending.profile import multi_v_die, tip_offset

off = tip_offset(75, 0.8)
PUNCH = {'id': 1, 'angle': 75, 'pts': [[x, y - off, r, a] for x, y, r, a in m.P97_PROFILE]}
DIES = [{'id': 2, 'v': v, 'angle': 85, 'pts': multi_v_die(m.DIE_SIZE, m.M460R_SIDES)} for v in (16, 22, 35, 50)]

# one bend: trivially free
r = plan([{'pos': 30, 'angle': 90, 'dir': 'up'}], 100, 2, [PUNCH], DIES)
assert r['ok'] and [o[0] for o in r['order']] == [0]
# a U channel with a wide base (100 mm) and short flanges (20 mm): free
r = plan([{'pos': 20, 'angle': 90, 'dir': 'up'}, {'pos': 120, 'angle': 90, 'dir': 'up'}], 140, 2, [PUNCH], DIES)
assert r['ok'] and len(r['order']) == 2
# a deep channel (30 mm base, 80 mm flanges): the second flange cannot get past the punch
r = plan([{'pos': 80, 'angle': 90, 'dir': 'up'}, {'pos': 110, 'angle': 90, 'dir': 'up'}], 190, 2, [PUNCH], DIES)
assert not r['ok'] and r['blocked']
print('ok')

# only the tools the caller can use: the V the stock does not cover is skipped even if it is the nearest one
DIE_FAR = {'id': 3, 'v': 35, 'angle': 85, 'pts': multi_v_die(m.DIE_SIZE, m.M460R_SIDES)}
r = plan([{'pos': 30, 'angle': 90, 'dir': 'up'}], 100, 2, [PUNCH], [DIES[0], DIE_FAR], usable=lambda i, p, d: d['id'] == 3)
assert r['ok'] and r['order'][0][2]['id'] == 3
r = plan([{'pos': 30, 'angle': 90, 'dir': 'up'}], 100, 2, [PUNCH], DIES, usable=lambda i, p, d: False)
assert not r['ok']                                                   # nothing usable: no plan from these tools (the caller then tries the whole library)
print('ok usable')
