"""Bend sequence for a flat pattern (no Flask).

Safety first: at every bend the LONG part of the sheet is on the operator's side and the SHORT part goes to the back gauge, so X is always
the shorter distance from the bend line to an end of the flat (`back_end` says which end rests on the fingers). Bends are made from the
outside in (shortest X first); the part is turned over when the fold direction changes and turned end-for-end when the gauge end changes.
ponytail: developed lengths only, no collision check - real sequencing needs the 3D part and tool shapes (stage 3).
"""

END = {'left': 'ляв', 'right': 'десен'}


def steps_from_bends(bends, flat):
    """bends: [{pos (mm from the left end of the flat), angle, dir 'up'|'down', note (optional)}] -> [{seq, angle_deg, x_mm, flip, notes, back_end, bend_dir}]."""
    rows = []
    for b in bends:
        left_first = b['pos'] <= flat - b['pos']
        rows.append((min(b['pos'], flat - b['pos']), b['pos'], 'left' if left_first else 'right', b))
    rows.sort(key=lambda r: (r[0], r[1]))
    steps, prev_dir, prev_end = [], None, None
    for i, (x, pos, end, b) in enumerate(rows, 1):
        notes = [b['note']] if b.get('note') else []
        if prev_end is None:
            notes.append(f'Късата страна към упора ({END[end]} край на развивката)')
        elif end != prev_end:
            notes.append(f'Завъртете детайла на 180°: късата страна към упора ({END[end]} край)')
        steps.append({'seq': i, 'angle_deg': b['angle'], 'x_mm': round(x, 1), 'flip': prev_dir is not None and b['dir'] != prev_dir,
                      'notes': '; '.join(notes) or None, 'back_end': end, 'bend_dir': b['dir']})
        prev_dir, prev_end = b['dir'], end
    return steps


def steps_from_unfold(unfold):
    """flashing-style unfold dict -> steps (see steps_from_bends)."""
    return steps_from_bends([{'pos': b['line'], 'angle': b['angle'], 'dir': b['dir'], 'note': 'Подгъвка (затваряне на ръба)' if b['kind'] == 'hem' else None}
                             for b in unfold['bends']], unfold['flat_length'])
