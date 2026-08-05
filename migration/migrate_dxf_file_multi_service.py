"""
One-off schema migration: drops dxf_file.service_id (the old single-service
FK added by migrate_services_pricing_refactor.py) now that the DXF
calculator prices a job against a list of services at once - see
DxfFile.services / dxf_file_service in app.py, and the checkbox picker on
upload.html.

The `dxf_file_service` link table itself is new - db.create_all() already
creates it, no ALTER needed. Run `python app.py` once first, then this
migration:

    python -m migration.migrate_dxf_file_multi_service

Safe to run more than once (DROP COLUMN IF EXISTS). A brand-new database
doesn't need this at all - db.create_all() already creates the current
schema directly. Any pre-existing per-file service reference is dropped
without a backfill into dxf_file_service - it was purely informational
(DxfFile.calculated_price is already a frozen historical value either way),
not worth the complexity of migrating a single FK into a many-to-many row.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('ALTER TABLE dxf_file DROP COLUMN IF EXISTS service_id'))
    db.session.commit()

print("dxf_file.service_id dropped (or already gone).")
