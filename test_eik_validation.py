"""Self-check for _validate_eik() - ЕИК/Булстат must be exactly 9 digits
when provided, and is optional otherwise (Client/Deliverer/Supplier all
route through this one helper).

    python test_eik_validation.py
"""
from app import _validate_eik

assert _validate_eik('') == (None, None), "blank EIK is optional, not an error"
assert _validate_eik(None) == (None, None)
assert _validate_eik('  ') == (None, None)

assert _validate_eik('203040557') == ('203040557', None), "9 digits is valid"

value, error = _validate_eik('20304055')  # 8 digits
assert value is None and error, "too short must be rejected"

value, error = _validate_eik('2030405577')  # 10 digits
assert value is None and error, "too long must be rejected"

value, error = _validate_eik('BG203040557')  # old VAT-style format, not a bare EIK
assert value is None and error, "non-numeric characters must be rejected"

print("ok")
