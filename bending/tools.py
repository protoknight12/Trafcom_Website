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
