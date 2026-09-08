"""
One-off schema migration for the APPORO-style "Inspection Report of
Quality" print layout (see admin_quality_check_print.html):

- QualityCheck gains order_no, disposition, supervisor_name.
- QualityMeasurement gains measurement_type (searchable dimension-category
  field, e.g. "Диаметър"/"Ъгъл"/"Дължина").

    python -m migration.migrate_quality_check_report_fields

Safe to run more than once.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS order_no VARCHAR(50)
    '''))
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS disposition VARCHAR(100)
    '''))
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS supervisor_name VARCHAR(100)
    '''))
    db.session.execute(text('''
        ALTER TABLE quality_measurement ADD COLUMN IF NOT EXISTS measurement_type VARCHAR(50)
    '''))
    db.session.commit()

print("Quality report-fields schema migration applied.")
