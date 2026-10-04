"""
One-time data step (run by migration/run_once.py): every panel / convector / sensor / battery stack / inverter / network device that is not on
the hall plan yet (e.g. the panels loaded by data_import_panels, which only creates ElectricalPanel rows) gets its plan object, so it shows in
the plan editor and in 3D (inside its room, else on the service strip behind the back wall). Devices already on the plan are not touched.

    python -m migration.data_hall_place_devices
"""
from app import app, db, _hall_auto_place

with app.app_context():
    placed = _hall_auto_place(keep=True)
    db.session.commit()

print(f"devices placed on the hall plan: {placed}")
