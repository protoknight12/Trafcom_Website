"""
One-time data step (run by migration/run_once.py): ClientDeliveryNote.kind 'return' used to mean "issue or return to
the client". It is now split (owner's decision 2026-11-17): 'issue' (издаване, the default) and 'return' (връщане).
Old 'return' notes with any detail/product line were goods issued to the client -> 'issue'; a note with material lines
only (the client's stored material given back) stays 'return'. Stock is not touched - both kinds move it the same way.

    python -m migration.data_client_note_issue_kind
"""
from app import app, db, ClientDeliveryNote

with app.app_context():
    changed = 0
    for note in ClientDeliveryNote.query.filter_by(kind='return'):
        if any(item.target_type != 'material' for item in note.items):
            note.kind = 'issue'
            changed += 1
    db.session.commit()

print(f'client delivery notes: {changed} set to issue')
