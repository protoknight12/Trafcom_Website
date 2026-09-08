"""
One-off schema migration: creates the PanelComponent/PanelWire tables (via
db.create_all()) - the internal one-line schematic editor for an
ElectricalPanel (breakers/fuses/contactors/etc., optionally wired to a real
Machine/child ElectricalPanel/ModbusDevice) - see /admin/panels/<id>/schematic
and PanelComponent's docstring in app.py.

    python -m migration.seed_panel_schematic

Safe to run more than once.
"""
from app import app, db

with app.app_context():
    db.create_all()

print("panel_component/panel_wire tables created (or already existed).")
