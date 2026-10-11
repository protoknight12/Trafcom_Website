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
