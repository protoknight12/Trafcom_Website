"""
One-off schema migration for the realistic hall 3D scene: hall_machine.model/rotation and
hall_shape.model/rotation and hall_equipment.rotation and hall_machine.acc_length/acc_width/acc_height/acc_side/acc_name (a machine's separately sized, named accessory) (which 3D model a machine/prop uses and which way its front faces).
Existing machines get the plan's default look (see HALL_LOOK_SEED in app.py) where they have none yet.
Safe to run more than once.

    python -m migration.migrate_add_hall_model
"""
from sqlalchemy import text

from app import app, db, HallMachine, HALL_LOOK_SEED

with app.app_context():
    db.create_all()
    for stmt in (
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS model VARCHAR(30)",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS rotation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS model VARCHAR(30)",
        "ALTER TABLE hall_shape ADD COLUMN IF NOT EXISTS rotation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE hall_equipment ADD COLUMN IF NOT EXISTS rotation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS acc_length FLOAT NOT NULL DEFAULT 0",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS acc_width FLOAT NOT NULL DEFAULT 0.7",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS acc_height FLOAT NOT NULL DEFAULT 0.8",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS acc_side VARCHAR(10) NOT NULL DEFAULT 'left'",
        "ALTER TABLE hall_machine ADD COLUMN IF NOT EXISTS acc_name VARCHAR(100)",
    ):
        db.session.execute(text(stmt))
    db.session.commit()
    for m in HallMachine.query.filter(HallMachine.acc_length > 0, HallMachine.acc_name.is_(None)):
        m.acc_name = 'Прътоподавател' if m.model == 'bar_lathe' else 'Инвентар'
    db.session.commit()
    for m in HallMachine.query.filter(HallMachine.model.is_(None)):
        if m.no in HALL_LOOK_SEED:
            m.model, m.rotation = HALL_LOOK_SEED[m.no]
    db.session.commit()

print("hall model/rotation columns added (or already existed).")
