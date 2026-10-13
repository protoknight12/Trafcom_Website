"""Bend sequence for a flat pattern (no Flask).

Safety first: at every bend the LONG part of the sheet is on the operator's side and the SHORT part goes to the back gauge, so X is always
the shorter distance from the bend line to an end of the flat (`back_end` says which end rests on the fingers). Bends are made from the
outside in (shortest X first) unless a collision-free order is searched (bending.planner); the part is turned over when the fold direction
changes and turned end-for-end when the gauge end changes.
ponytail: developed lengths only - real sequencing with 3D collisions is stage 3.
"""

import json

END = {'left': 'ляв', 'right': 'десен'}


def back_end_of(pos, flat):
    """The SHORT side goes to the back gauge: 'left' when the bend line is nearer to the left end of the flat."""
    return 'left' if pos <= flat - pos else 'right'


def steps_from_bends(bends, flat, order=None):
    """bends: [{pos (mm from the left end of the flat), angle, dir 'up'|'down', note (optional)}] -> [{seq, angle_deg, x_mm, flip, notes, back_end, bend_dir, length_mm (when the bend has a 'length')}].
    `order` = indexes into bends (default: outside in, shortest X first)."""
    rows = [(min(b['pos'], flat - b['pos']), b['pos'], back_end_of(b['pos'], flat), b) for b in bends]
    rows = [rows[i] for i in order] if order is not None else sorted(rows, key=lambda r: (r[0], r[1]))
    steps, prev_dir, prev_end = [], None, None
    for i, (x, pos, end, b) in enumerate(rows, 1):
        notes = [b['note']] if b.get('note') else []
        if prev_end is None:
            notes.append(f'Късата страна към упора ({END[end]} край на развивката)')
        elif end != prev_end:
            notes.append(f'Завъртете детайла на 180°: късата страна към упора ({END[end]} край)')
        steps.append({'seq': i, 'angle_deg': b['angle'], 'x_mm': round(x, 1), 'flip': prev_dir is not None and b['dir'] != prev_dir,
                      'notes': '; '.join(notes) or None, 'back_end': end, 'bend_dir': b['dir'], **({'length_mm': b['length']} if b.get('length') else {}), **({'inner_radius_mm': b['radius']} if b.get('radius') else {}), **({'line_json': json.dumps(b['line'])} if b.get('line') else {})})
        prev_dir, prev_end = b['dir'], end
    return steps


def steps_from_unfold(unfold):
    """flashing-style unfold dict -> steps (see steps_from_bends)."""
    # a tapered part's bend lines are slanted (y = 0 -> y = length): the step stands at the middle of its line, like the DXF-based steps
    L, f0, f1 = unfold.get('length') or 0.0, unfold['flat_length'], unfold.get('flat_length_end', unfold['flat_length'])
    return steps_from_bends([{'pos': (b['line'] + b.get('line_end', b['line'])) / 2, 'angle': b['angle'], 'dir': b['dir'], 'note': 'Подгъвка (затваряне на ръба)' if b['kind'] == 'hem' else None,
                              **({'radius': b['radius']} if b['kind'] == 'bend' and b.get('radius') else {}), **({'line': [b['line'], -L / 2, b.get('line_end', b['line']), L / 2]} if L else {})}
                             for b in unfold['bends']], (f0 + f1) / 2)


def legs_around(here, done, flat):
    """Legs of the part around the bend `here` with the bends in `done` already made. All are dicts {pos, angle, dir}; `here` also has `end` (the
    gauge end). -> (front, back): legs towards the operator / the back gauge, each [(length, curl towards the punch or None)]."""
    back_sign = -1 if here['end'] == 'left' else 1

    def legs(sign):   # sign -1: towards the left end of the flat, +1: towards the right end
        end = 0.0 if sign < 0 else flat
        ones = sorted((b for b in done if (b['pos'] - here['pos']) * sign > 1e-6), key=lambda b: abs(b['pos'] - here['pos']))
        out, prev = [], 0.0
        for dist, b in [(abs(b['pos'] - here['pos']), b) for b in ones] + [(abs(end - here['pos']), None)]:
            out.append((dist - prev, b['angle'] * (1 if b['dir'] == here['dir'] else -1) if b else None))
            prev = dist
        return out
    return legs(-back_sign), legs(back_sign)
