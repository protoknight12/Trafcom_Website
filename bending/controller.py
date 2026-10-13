"""The press brake controller's (Delem DA-69S) axis arithmetic, as the machine's own product files show it (no Flask). Everything here is checked against the
real products of the AD-ES 2040 backup (see docs/AD_ES_2040_CONTROLLER.md); what the files do not show is not modelled.

Y runs DOWN: Y grows as the beam goes down. `y_zero` = ToolReference - punch height - die height is the Y at which the punch tip touches the die top (no sheet).
The beam switches from fast closing to pressing speed at y_zero - thickness - mute (the files: 338 - 97.59558 - 80 - 1 - 4 = 155.40442, exactly the stored MutePosition).
The bottom of an air bend is `depth` below the die's shoulder plane: y_zero + depth (derived: the products store 0 there - the controller computes it when it runs).
R (finger height above the table): the finger sits at the die's top plane when R = die height (+0.001): the stored R of every flat first bend is 80.00095 for the 80 mm die
and 120.0009 for a 120 mm die. A finger lowered by its first level (a flange hanging down) is that much lower.
"""

MUTE_MM = 4.0           # DieMute of the dies / "Minimum relative mute" of the machine
R_EPS = 0.001           # the controller adds a hair to the die height


def y_positions(tool_ref, punch_h, die_h, thickness, depth, mute=MUTE_MM):
    """-> {'zero', 'mute', 'contact', 'bottom'}: Y values in mm (Y grows downwards)."""
    zero = tool_ref - punch_h - die_h
    return {'zero': round(zero, 3), 'mute': round(zero - thickness - mute, 3), 'contact': round(zero - thickness, 3), 'bottom': round(zero + depth, 3)}


def r_default(die_h, finger_option='face', first_level_mm=14.0):
    """The R that puts the finger where the sheet end meets it: at the die's top plane for the front-face contact, one first level lower for a hanging flange
    or for the ledge that goes under the sheet."""
    return round(die_h + R_EPS - (first_level_mm if finger_option in ('hang', 'ledge') else 0.0), 3)
