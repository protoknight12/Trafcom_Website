"""Self-check for two helpers deduplicated out of app.py (see the ponytail
audit): _save_upload() - the shared uuid-prefixed collision-safe file save,
now used by machine-card images, product images, and detail DXF uploads -
and _parse_machine_ids() - the shared "machine_ids" multi-select parser now
used by both the services and power-device admin routes.

    python -m testing.test_upload_and_machine_helpers
"""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from app import _save_upload, _parse_machine_ids


class _FakeFile:
    def __init__(self, filename):
        self.filename = filename
        self.saved_to = None

    def save(self, path):
        self.saved_to = path


class _FakeForm:
    def __init__(self, values):
        self._values = values

    def getlist(self, key):
        return self._values


# _save_upload: no file submitted -> None, nothing saved.
assert _save_upload(None, 'somedir') is None

# _save_upload: extension not allowed -> None, nothing saved.
blocked = _FakeFile('virus.exe')
assert _save_upload(blocked, 'somedir', {'png', 'jpg'}) is None
assert blocked.saved_to is None

# _save_upload: allowed extension -> uuid-prefixed name, saved under folder.
photo = _FakeFile('photo.png')
name = _save_upload(photo, 'somedir', {'png', 'jpg'})
assert name is not None and name.endswith('_photo.png'), name
assert len(name) == 32 + 1 + len('photo.png'), name  # 32 hex chars + '_' + original name
assert photo.saved_to == os.path.join('somedir', name)

# _save_upload: allowed_extensions=None means "accept anything" (detail DXF uploads).
anything = _FakeFile('notes.txt')
assert _save_upload(anything, 'somedir') is not None

# _parse_machine_ids: de-dupes, ignores blanks/non-numeric, preserves order.
form = _FakeForm(['3', '3', ' 5 ', 'not-a-number', '', '7'])
assert _parse_machine_ids(form) == [3, 5, 7], _parse_machine_ids(form)

# _parse_machine_ids: nothing selected is valid, not an error.
assert _parse_machine_ids(_FakeForm([])) == []

print('All upload/machine-id helper checks passed.')
