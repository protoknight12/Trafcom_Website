"""
One-time data step (run by migration/run_once.py): the owner decided (2026-10-15) that new accounts are created only by
an admin - so registration is switched off once, the same way the checkbox on /admin/users does it
(EditableText 'settings.registration_closed' = '1'). Reopening it later from that checkbox is not undone by this step.

    python -m migration.data_close_registration
"""
from app import app, db, EditableText, _REGISTRATION_CLOSED_KEY

with app.app_context():
    row = db.session.get(EditableText, _REGISTRATION_CLOSED_KEY)
    if row:
        row.content = '1'
    else:
        db.session.add(EditableText(key=_REGISTRATION_CLOSED_KEY, content='1'))
    db.session.commit()

print('registration closed (settings.registration_closed = 1)')
