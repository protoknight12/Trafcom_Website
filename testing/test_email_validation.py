"""Self-check for _validate_email() - blank is valid (email is optional on
the model for pre-existing rows), but if provided must look like an email.
Callers that require it (e.g. /register) check for a blank result themselves.

    python -m testing.test_email_validation
"""
from app import _validate_email

assert _validate_email('') == (None, None), "blank email is optional, not an error"
assert _validate_email(None) == (None, None)
assert _validate_email('   ') == (None, None)

assert _validate_email('User@Example.com') == ('user@example.com', None), "lowercased"

value, error = _validate_email('not-an-email')
assert value is None and error, "missing @ must be rejected"

value, error = _validate_email('user@nodot')
assert value is None and error, "missing TLD must be rejected"

value, error = _validate_email('user@ example.com')
assert value is None and error, "whitespace must be rejected"

print("ok")
