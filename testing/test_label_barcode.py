"""Self-check for the Code128 SVG barcode generation used by print_label()
in app.py. Imports the real generate_barcode_svg() instead of a copy, so it
can't silently drift from production behavior. Requires the project's
actual environment (Flask, python-barcode, etc.) - run with:

    "D:/python_3131_interpreter/Scripts/python.exe" test_label_barcode.py
"""
from app import generate_barcode_svg

# erp_number is stored as an int (see migrate_erp_number_unique_int.py) -
# this is the case that broke in production: python-barcode needs a str.
svg = generate_barcode_svg(3010001546)
assert svg.startswith('<svg'), "output should be a bare <svg> element, no XML prolog"
assert svg.strip().endswith('</svg>'), "output should be a complete svg element"
assert svg.count('<rect') > 10, "expected many bars, got almost none - encoding likely broken"

longer_svg = generate_barcode_svg(30100015469999)
assert len(longer_svg) > len(svg), "a longer code should encode to more/wider bars"

print("ok")
