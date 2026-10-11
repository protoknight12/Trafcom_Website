"""Collision-free bend plan (no Flask): order of the bends, punch and die for each, such that in the cross-section the part never runs into the
punch (bending.profile.step_collision), keeping the safety rule of bending.sequence (short side to the back gauge).
Depth-first search over the order with the shortest X first, preferring to keep the same tools; failed sets of done bends are remembered.
ponytail: cross-section only (no length-wise / 3D check), air bending, developed lengths, back gauge side fixed by the safety rule."""
from bending.profile import step_collision
from bending.sequence import back_end_of, legs_around
from bending.tools import recommended_v


def _combos(angle, thickness, punches, dies, limit=6):
    inner = 180 - angle
    ps = sorted((p for p in punches if p['angle'] is None or p['angle'] <= inner + 0.01), key=lambda p: -(p['angle'] or 0))
    vrec = recommended_v(thickness)
    ds = sorted((d for d in dies if d['angle'] is None or d['angle'] <= inner + 0.01), key=lambda d: abs(d['v'] - vrec))
    return [(p, d) for d in ds[:3] for p in ps[:2]][:limit]


def plan(bends, flat, thickness, punches, dies, max_nodes=30000, overbend=None):
    """overbend(v, thickness) -> springback degrees (default 0).
    bends: [{pos, angle, dir}]; punches: [{id, angle, pts}]; dies: [{id, v, angle, pts}].
    -> {'ok': True, 'order': [(bend index, punch, die)]} or {'ok': False, 'done': deepest partial order, 'blocked': [bend indexes with no free tool now]}"""
    here = [{**b, 'end': back_end_of(b['pos'], flat)} for b in bends]
    xs = [min(b['pos'], flat - b['pos']) for b in bends]
    sorted_idx = sorted(range(len(bends)), key=lambda i: (xs[i], bends[i]['pos']))
    cache, failed, nodes = {}, set(), [0]
    best = {'done': []}

    def free(i, done, p, d):
        key = (i, done, p['id'], d['id'], d['v'])
        if key not in cache:
            if bends[i]['angle'] >= 170:   # a hem: closed in a separate flattening step, not an air bend
                cache[key] = True
            else:
                front, back = legs_around(here[i], [bends[j] for j in done], flat)
                cache[key] = not step_collision(p['pts'], d['pts'], d['v'], thickness, bends[i]['angle'], front, back,
                                                overbend=overbend(d['v'], thickness) if overbend else 0.0)
        return cache[key]

    def dfs(done, prev):
        if len(done) == len(bends):
            return []
        if done in failed or nodes[0] > max_nodes:
            return None
        nodes[0] += 1
        for i in sorted_idx:
            if i in done:
                continue
            combos = _combos(bends[i]['angle'], thickness, punches, dies)
            combos.sort(key=lambda c: 0 if prev and (c[0]['id'], c[1]['id'], c[1]['v']) == prev else 1)   # keep the tools if possible
            for p, d in combos:
                if free(i, done, p, d):
                    rest = dfs(done | frozenset([i]), (p['id'], d['id'], d['v']))
                    if rest is not None:
                        return [(i, p, d)] + rest
                    break   # the later bends are stuck whatever tool this one uses: try another bend first
        if len(done) > len(best['done']):
            best['done'] = sorted(done)
        failed.add(done)
        return None

    res = dfs(frozenset(), None)
    if res is not None:
        return {'ok': True, 'order': res}
    done = frozenset(best['done'])
    blocked = [i for i in range(len(bends)) if i not in done and not any(free(i, done, p, d) for p, d in _combos(bends[i]['angle'], thickness, punches, dies))]
    return {'ok': False, 'done': sorted(done), 'blocked': blocked}
