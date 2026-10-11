"""Sheet-metal unfold core, shared by /flashing and the bending module (no Flask).

A part is a chain of walls joined by bends. Wall sizes are OUTSIDE dimensions (to the virtual sharp
corner). Flat length = straight parts + a bend allowance per bend, with outside setback
OSSB = (r + t) * tan(angle / 2). How the allowance is found is the *strategy*:
k_factor(k) -> BA = angle * (r + k*t); bd_table(lookup) -> BA = 2*OSSB - BD(angle, r, t).
"""
import math


def k_factor(k):
    """Strategy: bend allowance from a K-factor."""
    return lambda angle, r, t, setback: math.radians(angle) * (r + k * t)


def bd_table(lookup):
    """Strategy: bend allowance from a bend-deduction table; lookup(angle, r, t) -> BD or None (unknown)."""
    def allowance(angle, r, t, setback):
        bd = lookup(angle, r, t)
        if bd is None:
            raise ValueError(f'Няма таблична стойност за огъване {angle}°, R{r}, s{t}.')
        return 2 * setback - bd
    return allowance


def unfold_section(outs, angles, radii, kinds, signs, t, strategy, labels, is_hem, inside=False, hem_sign_ends=(1, 1)):
    """One cross-section -> flat layout along X, bend table, sharp-corner profile.
    outs: wall sizes (n); angles/radii/kinds/signs: per bend (n-1; kind 'bend' or 'hem', sign +1 up / -1 down);
    is_hem/labels: per wall; inside: sizes were given to the inner virtual sharp; hem_sign_ends: knot offset
    side for a hem at the start / end of the chain."""
    outs = list(outs)
    n = len(outs)
    given = outs[:]
    if inside:
        # Each real bend adds t*tan(angle/2) per side to get the outside size.
        outs = [o + sum(t * math.tan(math.radians(angles[b]) / 2) for b in (j - 1, j) if 0 <= b < n - 1 and kinds[b] == 'bend')
                for j, o in enumerate(outs)]
    setback = [(radii[i] + t) * (math.tan(math.radians(angles[i]) / 2) if angles[i] < 180 else 1) for i in range(n - 1)]
    allowance = [strategy(angles[i], radii[i], t, setback[i]) for i in range(n - 1)]
    # A side that carries a hem keeps its stated size WITH the hem: its end is the
    # farthest point of side+hem along the side (fold apex, or the leg's tip when
    # the hem is opened less than 90°), so 50 stays 50 with or without a hem.
    side_setback = setback[:]
    for i in range(n - 1):
        if kinds[i] == 'hem':
            a_rad = math.radians(angles[i])
            leg = max(outs[i] if is_hem[i] else outs[i + 1], 0) - setback[i]
            side_setback[i] = (radii[i] + t) if angles[i] >= 90 else (radii[i] + t) * math.sin(a_rad) + max(leg, 0) * math.cos(a_rad)
    flanges, bends, x = [], [], 0.0
    trim = []  # (setback at the flange's start, at its end) - where the straight part sits on the sharp-corner profile
    for j in range(n):
        sb = setback if is_hem[j] else side_setback
        trim.append((sb[j - 1] if j else 0, sb[j] if j < n - 1 else 0))
        straight = outs[j] - trim[j][0] - trim[j][1]
        if straight < 0:
            raise ValueError(f'Страна {labels[j]} е твърде къса за радиуса/дебелината на съседните огъвки.')
        flanges.append({'n': labels[j], 'given': given[j], 'outside': round(outs[j], 3), 'straight': round(straight, 3), 'x0': round(x, 3), 'x1': round(x + straight, 3)})
        x += straight
        if j < n - 1:
            bends.append({
                'n': j + 1, 'kind': kinds[j], 'angle': angles[j], 'dir': 'up' if signs[j] > 0 else 'down',
                'radius': radii[j], 'allowance': round(allowance[j], 3), 'deduction': round(2 * setback[j] - allowance[j], 3),
                'x0': round(x, 3), 'x1': round(x + allowance[j], 3), 'line': round(x + allowance[j] / 2, 3),
            })
            x += allowance[j]

    # Cross-section with sharp corners (preview only).
    pos, heading, profile, headings = (0.0, 0.0), 0.0, [(0.0, 0.0)], []
    for j in range(n):
        headings.append(heading)
        pos = (pos[0] + outs[j] * math.cos(heading), pos[1] + outs[j] * math.sin(heading))
        profile.append((round(pos[0], 3), round(pos[1], 3)))
        if j < n - 1:
            heading += signs[j] * math.radians(angles[j])

    # Knots [flat x, profile x, profile y] at each straight part's start/end: they map a point of the flat
    # pattern onto the bent part (bend zones = straight chord between knots) for the isometric view. A hem
    # lies one thickness beside the flange it folds onto, so it doesn't draw on top of it.
    knots = []
    for j in range(n):
        ux, uy = math.cos(headings[j]), math.sin(headings[j])
        off = 0.0
        if is_hem[j]:
            off = -hem_sign_ends[0] * t if j == 0 else hem_sign_ends[1] * t
        ox, oy = -uy * off, ux * off
        a, b = profile[j], profile[j + 1]
        knots.append([flanges[j]['x0'], a[0] + ux * trim[j][0] + ox, a[1] + uy * trim[j][0] + oy])
        knots.append([flanges[j]['x1'], b[0] - ux * trim[j][1] + ox, b[1] - uy * trim[j][1] + oy])
    return {'flanges': flanges, 'bends': bends, 'flat': round(x, 3), 'profile': profile, 't': t,
            'knots': [[round(v, 3) for v in k] for k in knots]}
