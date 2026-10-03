"""
One-time data step for the unified hall map (run by migration/run_once.py from deploy/update.sh, exactly once per database):
creates the Machine of every dossier on the plan, the Room of every marked room, puts every panel / convector / sensor / battery stack /
inverter / network device that is not on the plan yet onto it, and fills in the room-map and distribution-scheme positions that are
still empty. Anything that already has a room and a position on its map is left exactly where it is (the "Синхронизирай картите"
button on the plan tab is the one that overwrites).

    python -m migration.data_hall_initial_sync
"""
from app import app, db, _hall_sync_maps, _hall_auto_place

with app.app_context():
    out = _hall_sync_maps(keep=True)
    placed = _hall_auto_place(keep=True)
    db.session.commit()

print(f"hall maps synced: {out['rooms']} rooms, {out['machines']} machines, {out['panels']} panels created; {placed} devices placed on the plan.")
