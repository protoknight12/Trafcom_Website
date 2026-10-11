"""
One-off schema migration: links BendProgram to the ControlledDocument created when it is approved
(only needed if bend_program already existed before this column was added).

    python -m migration.migrate_add_bend_program_document

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text("ALTER TABLE bend_program ADD COLUMN IF NOT EXISTS document_id INTEGER REFERENCES controlled_document(id)"))
    db.session.commit()

print("bend_program.document_id added (or already existed).")
