"""Bending force and die choice for air bending (no Flask). Force: F = 1.42 * Rm * s^2 / V  [kN/m] (Rm in N/mm^2, s and V in mm)."""

G = 9.80665  # tonnes-force -> kN


def force_kn_per_m(rm, thickness, v):
    """Air-bending force per metre of bend length for a 90 degree bend."""
    if min(rm, thickness, v) <= 0:
        raise ValueError('Якост, дебелина и V-отвор трябва да са положителни.')
    return 1.42 * rm * thickness ** 2 / v


def recommended_v(thickness):
    """Usual V-opening rule of thumb: 6s up to 3 mm, 8s up to 10 mm, 10s above."""
    if thickness <= 0:
        raise ValueError('Дебелината трябва да е положителна.')
    return thickness * (6 if thickness <= 3 else 8 if thickness <= 10 else 10)


def rank_dies(dies, rm, thickness, length_mm, machine_kn=None):
    """dies: [(code, v_opening, max_kn_per_m or None)]. Returns [{code, v, force_kn_per_m, total_kn, ok, notes}] ordered by
    closeness of V to the recommended one; ok = the tool's own limit (when known) and the machine's tonnage both hold."""
    vrec = recommended_v(thickness)
    out = []
    for code, v, cap in dies:
        if not v:
            continue
        f = force_kn_per_m(rm, thickness, v)
        total = f * length_mm / 1000
        notes = []
        if cap is not None and f > cap:
            notes.append(f'над допустимото за инструмента ({cap:g} kN/m)')
        if machine_kn is not None and total > machine_kn:
            notes.append(f'над тонажа на машината ({machine_kn:.0f} kN)')
        if v < 5 * thickness:
            notes.append('V-отворът е твърде малък за дебелината')
        out.append({'code': code, 'v': v, 'force_kn_per_m': round(f, 1), 'total_kn': round(total, 1), 'ok': not notes, 'notes': notes})
    return sorted(out, key=lambda d: (not d['ok'], abs(d['v'] - vrec)))


def air_bend_depth(v, thickness, fold_deg):
    """Air bending: how far the punch tip goes below the die's shoulder plane, and the whole travel from first touch of the sheet.
    fold_deg = bend (deflection) angle, 90 = a right angle. Sheet legs straight from the shoulders (shoulder/tip radii ignored: +-0.3 mm).
    -> (depth_mm, travel_mm)"""
    import math
    if not 0 < fold_deg < 180 or min(v, thickness) <= 0:
        raise ValueError('Ъгълът трябва да е между 0 и 180°, V и дебелината - положителни.')
    half = math.radians((180 - fold_deg) / 2)  # half of the inner angle
    depth = v / 2 / math.tan(half) - thickness / math.sin(half)
    return round(depth, 2), round(depth + thickness, 2)


def step_checks(fold_deg, thickness, v, punch_angle=None, die_angle=None, stroke_mm=None, punch_h=None, die_h=None, daylight_mm=None):
    """Warnings (Bulgarian) for one bend: can the tools make this angle, and does the press reach it."""
    out = []
    inner = 180 - fold_deg
    for name, a in (('поансонът', punch_angle), ('V-каналът', die_angle)):
        if a is not None and a > inner + 0.01:
            out.append(f'Ъгълът на {name} ({a:g}°) е по-голям от вътрешния ъгъл на огъването ({inner:g}°).')
    if v and thickness:
        depth, travel = air_bend_depth(v, thickness, fold_deg)
        if depth < 0:
            out.append('Ъгълът не се достига при този V-отвор (пробив под равнината на рамената).')
        if stroke_mm is not None and travel > stroke_mm:
            out.append(f'Нужен ход {travel:g} mm, ходът на пресата е {stroke_mm:g} mm.')
        if daylight_mm is not None and punch_h and die_h and punch_h + die_h > daylight_mm:
            out.append(f'Поансон + матрица ({punch_h + die_h:g} mm) са повече от отварянето на пресата ({daylight_mm:g} mm).')
    return out


def pick_tools(punches, dies, thickness, fold_deg):
    """Default tools for a bend. punches: [(id, angle_deg)], dies: [(id, v, angle_deg)] (one row per V of a multi-V die).
    Punch: the sharpest-fitting one (largest angle that still fits the inner angle); die: V closest to the usual rule for the thickness
    among those whose groove angle fits. -> (punch_id or None, (die_id, v) or None)"""
    inner = 180 - fold_deg
    fit_p = [p for p in punches if p[1] is None or p[1] <= inner + 0.01]
    punch = max(fit_p, key=lambda p: p[1] or 0)[0] if fit_p else None
    vrec = recommended_v(thickness)
    fit_d = [d for d in dies if d[2] is None or d[2] <= inner + 0.01]
    die = min(fit_d, key=lambda d: abs(d[1] - vrec)) if fit_d else None
    return punch, ((die[0], die[1]) if die else None)


def springback_deg(material_name, v, thickness):
    """Estimated springback of an air bend (degrees the sheet opens after the punch lifts), so the tool has to go that much further. Base value by
    material from keywords of its name (stainless 3, aluminium 2.5, copper/brass 2, else steel 1.5), scaled with V/t (8 = the base). A rough
    estimate - confirm with a test bend and then correct it in the program."""
    name = (material_name or '').lower()
    base = 1.5
    if any(k in name for k in ('неръж', 'инокс', 'inox', 'aisi', 'stainless', '304', '316')):
        base = 3.0
    elif any(k in name for k in ('алум', 'alu')):
        base = 2.5
    elif any(k in name for k in ('мед', 'меси', 'copper', 'brass')):
        base = 2.0
    return round(base * max(0.5, min(2.0, (v / thickness) / 8)), 1)


def outside_length(leg, fold_deg, inner_radius, thickness, k=0.33):
    """Outside dimension of an end flange after the bend, from its developed length `leg` (bend line to the end, neutral fibre):
    leg - BA/2 + OSSB, BA = fold*(r + k*t), OSSB = (r + t)*tan(fold/2). The same rule as bending.unfold."""
    import math
    th = math.radians(fold_deg)
    return leg - th * (inner_radius + k * thickness) / 2 + (inner_radius + thickness) * math.tan(th / 2)


def pick_segments(length, avail, max_total=None):
    """Which tool segments to mount for a bend `length` mm long. avail: {segment length: how many pieces are available (the same length on the punch and
    on the die)}. The tool must cover the whole bend: the smallest total >= length (least overhang), then the fewest pieces; the pieces are laid out
    symmetrically about the middle, the longest in the centre. -> {'total', 'excess', 'segments': [lengths left to right]} or None when it cannot be covered.
    ponytail: all pieces cut at multiples of 5 mm (the real set is 10/15/20/40/50/100/200/370/415), the bend length = the part's width."""
    step = 5
    items = sorted((int(round(l)), n) for l, n in avail.items() if n > 0 and l > 0)
    cap = int((max_total if max_total else sum(l * n for l, n in items)) // step)
    need = int(-(-length // step))
    best = {0: []}   # reachable total (in steps) -> the fewest pieces reaching it
    for l, n in items:
        for _ in range(int(n)):
            for t, segs in sorted(best.items(), reverse=True):
                u = t + l // step
                if u <= cap and (u not in best or len(best[u]) > len(segs) + 1):
                    best[u] = segs + [l]
    fit = [t for t in best if t >= need]
    if not fit:
        return None
    t = min(fit, key=lambda k: (k, len(best[k])))
    segs = sorted(best[t], reverse=True)
    left, right = [], []
    for i, l in enumerate(segs):   # the longest in the centre, the others alternately outwards
        (left if i % 2 else right).append(l)
    return {'total': t * step, 'excess': round(t * step - length, 1), 'segments': left[::-1] + right}
