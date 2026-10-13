"""
One-off migration: BendStep.inner_radius_mm - the inner radius of the bend in the part's model (the STEP import writes it into the DXF layer name,
BEND_UP_90_R2). Used instead of the air-bend rule 0.16 V in the step pictures, collision checks, 3D simulation and the tool choice.

    python -m migration.migrate_add_bend_step_radius

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE bend_step ADD COLUMN IF NOT EXISTS inner_radius_mm DOUBLE PRECISION'))
    db.session.commit()
    print('bend_step.inner_radius_mm is ready.')
