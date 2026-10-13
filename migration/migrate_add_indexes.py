"""
One-off schema migration: indexes on the foreign keys and filter columns the app joins / filters / orders on (PostgreSQL does not
index foreign keys by itself), plus (host, ts)-style composites on the per-device reading logs. The names are the ones SQLAlchemy
gives index=True / db.Index(...) in app.py (ix_<table>_<column>), so a fresh db.create_all() and this script end up identical.
Safe to run more than once (CREATE INDEX IF NOT EXISTS). A brand-new database doesn't need it.

    python -m migration.migrate_add_indexes
"""
from sqlalchemy import text

from app import app, db

INDEXES = (  # (table, columns)
    ('activity_log', ('timestamp',)),
    ('dxf_file', ('user_id',)),
    ('detail', ('owner_client_id',)),
    ('detail_dxf_file', ('detail_id',)),
    ('operation', ('detail_id',)),
    ('product', ('owner_client_id',)),
    ('product_image', ('product_id',)),
    ('product_extra_cost', ('product_id',)),
    ('product_detail', ('product_id',)),
    ('product_detail', ('detail_id',)),
    ('order', ('user_id',)),
    ('order', ('status',)),
    ('order', ('client_id',)),
    ('order_item', ('order_id',)),
    ('order_item_component', ('order_item_id',)),
    ('order_item_operation', ('order_item_id',)),
    ('delivery_note_item', ('delivery_note_id',)),
    ('delivery_note_item', ('target_type', 'target_id')),
    ('client_delivery_note_item', ('client_delivery_note_id',)),
    ('client_delivery_note_item', ('target_type', 'target_id')),
    ('offer_item', ('offer_id',)),
    ('shelly_reading_log', ('host', 'ts')),
    ('solis_reading_log', ('device_id', 'ts')),
    ('temperature_reading', ('sensor_id', 'ts')),
    ('convector_log', ('convector_id', 'ts')),
)

with app.app_context():
    for table, cols in INDEXES:
        name = 'ix_%s_%s' % (table, '_'.join(cols))
        if cols == ('target_type', 'target_id'):
            name = 'ix_%s_target' % table   # the one composite whose name does not spell out its columns
        db.session.execute(text('CREATE INDEX IF NOT EXISTS %s ON "%s" (%s)' % (name, table, ', '.join(cols))))
    db.session.commit()

print("%d indexes created (or already there)." % len(INDEXES))
