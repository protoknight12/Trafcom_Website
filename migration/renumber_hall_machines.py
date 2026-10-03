"""
One-off: renumbers all hall machines 1..N in their current number order (unnumbered ones last), so the plan
labels in /factory3d and /admin/hall are sequential. Safe to run more than once. (The same renumbering is
available as the "Преномерирай" button on /admin/hall.)

    python -m migration.renumber_hall_machines
"""
from app import app, db, HallMachine

with app.app_context():
    machines = HallMachine.query.order_by(HallMachine.no.is_(None), HallMachine.no, HallMachine.id).all()
    for i, m in enumerate(machines, 1):
        m.no = i
    db.session.commit()
    print(f"{len(machines)} machines renumbered: " + ", ".join(f"{m.no}={m.name}" for m in machines))
