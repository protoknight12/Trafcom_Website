"""
One-off migration: BendStep.line_json - the bend line of the step in the flat pattern [c0, w0, c1, w1]. On a tapered part (a flashing cladding) the line is slanted, so
X differs along it: the program shows X1 and X2 for the two back-gauge fingers instead of one X for the middle of the line.

    python -m migration.migrate_add_bend_step_line

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE bend_step ADD COLUMN IF NOT EXISTS line_json TEXT'))
    db.session.commit()
    print('bend_step.line_json is ready.')
