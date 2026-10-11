"""
One-off schema migration: BendTool.profile_json - the real outline of a punch/die (JSON list of [x, y] in mm), drawn by the
bending module's profile picture and edited in the profile editor. and BendTool.base_height_mm (height of a die's 60 mm base). and BendStep.die_v_mm (which V of a multi-V die is up). Only needed if bend_tool already existed.

    python -m migration.migrate_add_bend_tool_profile

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text("ALTER TABLE bend_tool ADD COLUMN IF NOT EXISTS profile_json TEXT"))
    db.session.execute(text("ALTER TABLE bend_tool ADD COLUMN IF NOT EXISTS base_height_mm DOUBLE PRECISION"))
    db.session.execute(text("ALTER TABLE bend_step ADD COLUMN IF NOT EXISTS die_v_mm DOUBLE PRECISION"))
    db.session.commit()

print("bend_tool.profile_json / base_height_mm and bend_step.die_v_mm added (or already existed).")
