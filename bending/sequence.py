"""Bend sequence for a flat-pattern unfold (no Flask).

ponytail: simple rule, no collision check - bends left to right along the flat, back gauge against the far (still
flat) end, flip whenever the fold direction changes. Real sequencing with collisions needs the 3D part and tool shapes.
"""


def steps_from_unfold(unfold):
    """flashing-style unfold dict -> [{seq, angle_deg, x_mm, flip, notes}]."""
    flat, steps, prev = unfold['flat_length'], [], None
    for i, b in enumerate(unfold['bends'], 1):
        flip = prev is not None and b['dir'] != prev
        steps.append({'seq': i, 'angle_deg': b['angle'], 'x_mm': round(flat - b['line'], 1), 'flip': flip,
                      'notes': 'Подгъвка (затваряне на ръба)' if b['kind'] == 'hem' else None})
        prev = b['dir']
    return steps
