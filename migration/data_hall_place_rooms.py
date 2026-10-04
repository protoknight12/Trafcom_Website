"""
One-time data step (run by migration/run_once.py): every Room (a room map in "Сгради и помещения") must stand on the hall plan, so a
Room that has no marked-room shape yet is placed there (_hall_place_room: 8 x 6 m on a free spot, with its building and walls). Its devices
keep their room. Rooms already on the plan are left exactly as they are.

    python -m migration.data_hall_place_rooms
"""
from app import app, db, Room, HallShape, _hall_place_room, _hall_fit_buildings

with app.app_context():
    placed = [r.name for r in Room.query.all() if not HallShape.query.filter_by(kind='room', room_id=r.id).first() and _hall_place_room(r)]
    _hall_fit_buildings()
    db.session.commit()

print(f"rooms placed on the hall plan: {len(placed)} {placed}")
