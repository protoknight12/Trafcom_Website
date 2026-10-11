"""Per-part bending time estimate (no Flask). Handling times are shop numbers that nobody has timed yet - the defaults are a starting point;
override any of them per machine in PressBrakeConfig.kinematics_json['timing'] after timing a few real runs."""

DEFAULTS = {
    'bend_s': 25.0,            # place the part on the gauge, press, take it off - per bend
    'flip_s': 10.0,            # turn the part over
    'load_s': 20.0,            # take the blank + put the finished part away - per part
    'tool_change_min': 8.0,    # another punch/die setup mid-program (sections arranged again)
    'batch_setup_min': 10.0,   # first setup, once per batch - NOT part of the per-part time
}


def estimate(bends, flips, tool_changes, params=None):
    """-> {'per_part_min', 'batch_setup_min', 'bend_min', 'flip_min', 'load_min', 'change_min'}; per_part_min is what an Operation of the Detail bills.
    A tool change mid-program is charged to each part, which overstates it when the batch bends all parts of one setup first - ponytail: no batching model."""
    p = {**DEFAULTS, **(params or {})}
    parts = {'bend_min': bends * p['bend_s'] / 60, 'flip_min': flips * p['flip_s'] / 60, 'load_min': p['load_s'] / 60 if bends else 0.0,
             'change_min': tool_changes * p['tool_change_min']}
    out = {k: round(v, 2) for k, v in parts.items()}
    out['per_part_min'] = round(sum(parts.values()), 2)
    out['batch_setup_min'] = p['batch_setup_min'] if bends else 0.0
    return out
