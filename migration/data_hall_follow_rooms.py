"""
One-time data step (run by migration/run_once.py): makes the hall plan agree with the rooms that devices were given on the forms.
  1. an explicit parent (parent_id) of a plan shape / machine / device that names a room or building shape that no longer exists is cleared;
  2. every machine, panel, convector, sensor and battery stack with a room whose plan object stands outside that room's area is moved
     to the room's centre (see _hall_follow_room, _hall_object_of).
Nothing is created or deleted.

    python -m migration.data_hall_follow_rooms
"""
from app import (app, db, HallShape, HallMachine, HallEquipment, Machine, ElectricalPanel, Convector, TemperatureSensor, BatteryStack,
                 _hall_follow_room)

with app.app_context():
    ids = {i for (i,) in db.session.query(HallShape.id)}
    cleared = 0
    for M in (HallShape, HallMachine, HallEquipment):
        for o in M.query.filter(M.parent_id.isnot(None), M.parent_id != 0):
            if o.parent_id not in ids:
                o.parent_id = None
                cleared += 1
    db.session.flush()
    moved = 0
    for M in (Machine, ElectricalPanel, Convector, TemperatureSensor, BatteryStack):
        for t in M.query.filter(M.room_id.isnot(None)):
            before = _hall_object_of(t)
            pos = (before.x, before.z) if before else None
            _hall_follow_room(t)
            if before and (before.x, before.z) != pos:
                moved += 1
    db.session.commit()

print(f"dangling parents cleared: {cleared}; objects moved into their room: {moved}")
