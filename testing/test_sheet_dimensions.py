from app import _parse_sheet_dimensions

# blank fields -> all None (sheet_length_mm, sheet_width_mm, thickness_mm, height_mm)
assert _parse_sheet_dimensions({}) == [None, None, None, None]

# valid values parsed as floats
assert _parse_sheet_dimensions({
    'sheet_length_mm': '3000', 'sheet_width_mm': '1500', 'thickness_mm': '2.5', 'height_mm': '40'
}) == [3000.0, 1500.0, 2.5, 40.0]

# partial input keeps the rest None
assert _parse_sheet_dimensions({'thickness_mm': '3'}) == [None, None, 3.0, None]

# negative and non-numeric input both raise ValueError
for bad in ({'sheet_length_mm': '-5'}, {'thickness_mm': 'abc'}):
    try:
        _parse_sheet_dimensions(bad)
        assert False, f"expected ValueError for {bad}"
    except ValueError:
        pass

print("ok")
