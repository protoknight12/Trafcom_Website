"""
One-off: seeds the Услуги/homepage machine-park cards (ServiceMachineCard table)
from SERVICE_MACHINE_CARDS_SEED / INDEX_MACHINE_CARDS_SEED in app.py.

Needed because that seeding normally only runs inside app.py's
`if __name__ == '__main__':` block (see the bottom of app.py) - a production
WSGI server (gunicorn, waitress) imports `app` as a callable and never
executes that block, so a database that was only ever initialized by a WSGI
deploy (never once by running `python app.py` directly) ends up with the
service_machine_card table created but empty - /services and / then render
with zero machine cards (just the "+ Добави машина" box for admins, nothing
at all for anonymous visitors).

Safe to run more than once - seed_service_machine_cards()/seed_index_machine_cards()
are themselves idempotent (skip seeding a page that already has at least one
card, so a deliberately-deleted card is never silently re-added).

    "D:/python_3131_interpreter/Scripts/python.exe" backfill_service_machine_cards.py
"""
from app import app, db, ServiceMachineCard, seed_service_machine_cards, seed_index_machine_cards

with app.app_context():
    db.create_all()
    seed_service_machine_cards()
    seed_index_machine_cards()
    services_count = ServiceMachineCard.query.filter_by(page='services').count()
    index_count = ServiceMachineCard.query.filter_by(page='index').count()

print(f"service_machine_card rows now: {services_count} for 'services', {index_count} for 'index'.")
