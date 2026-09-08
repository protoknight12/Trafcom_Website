"""
One-off schema migration for the QC module's batch/multi-sample redesign
(matches the shop's paper "Mechanical Inspection Report" form - see
admin_quality_check_print()):

- QualityCheck gains drawing_no, batch_size, sample_size (header fields).
- QualityMeasurement drops measured_value - replaced by the new
  QualitySample table (one row per sample per dimension), which
  db.create_all() already creates on its own since it didn't exist before.
  This only touches the two pre-existing tables.

    python -m migration.migrate_quality_batch_samples

Safe to run more than once. Any QualityMeasurement rows created before this
migration lose their old single measured_value (dropped, not migrated into
a QualitySample) - acceptable since this module has had no real production
data yet.
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS drawing_no VARCHAR(50)
    '''))
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS batch_size INTEGER
    '''))
    db.session.execute(text('''
        ALTER TABLE quality_check ADD COLUMN IF NOT EXISTS sample_size INTEGER NOT NULL DEFAULT 1
    '''))
    db.session.execute(text('''
        ALTER TABLE quality_measurement DROP COLUMN IF EXISTS measured_value
    '''))
    db.session.commit()

print("Quality batch/sample schema migration applied.")
