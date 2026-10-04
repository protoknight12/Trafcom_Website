"""
One-time data step (run by migration/run_once.py): a Room (map card) cannot exist without its marked room on the hall plan. Rooms whose
shape was deleted in the editor before that rule existed are removed here; their machines / convectors / sensors / battery stacks stay
but lose the room, their panels are deleted (same as the "Изтрий" button on the room list). The general "whole hall" room is kept.

    python -m migration.data_hall_drop_unplaced_rooms
"""
from app import app, db, Room, HallShape, HALL_ROOM_NAME, _remove_room

with app.app_context():
    gone = [r.name for r in Room.query.all() if r.name != HALL_ROOM_NAME and not HallShape.query.filter_by(kind='room', room_id=r.id).first()]
    for r in Room.query.filter(Room.name.in_(gone)).all():
        _remove_room(r)
    db.session.commit()

print(f"rooms without a place on the plan removed: {len(gone)} {gone}")
