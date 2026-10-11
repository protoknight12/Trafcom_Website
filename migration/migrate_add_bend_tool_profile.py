"""
One-off schema migration: BendTool.profile_json - the real outline of a punch/die (JSON list of [x, y] in mm), drawn by the
bending module's profile picture and edited in the profile editor. Only needed if bend_tool already existed.

    python -m migration.migrate_add_bend_tool_profile

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text("ALTER TABLE bend_tool ADD COLUMN IF NOT EXISTS profile_json TEXT"))
    db.session.commit()

print("bend_tool.profile_json added (or already existed).")
