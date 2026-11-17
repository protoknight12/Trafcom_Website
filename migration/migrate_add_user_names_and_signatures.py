"""
User.first_name / last_name (the name printed on documents instead of the username) and the editable Предал/Приел
names on delivery notes and client delivery notes (handed_by / received_by, NULL = the default). Safe to re-run.

    python -m migration.migrate_add_user_names_and_signatures
"""
from sqlalchemy import text

from app import app, db

with app.app_context():
    for column in ('first_name', 'last_name'):
        db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS {column} VARCHAR(50)'))
    for table in ('delivery_note', 'client_delivery_note'):
        for column in ('handed_by', 'received_by'):
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} VARCHAR(150)'))
    db.session.commit()

print('user first_name/last_name, delivery note handed_by/received_by: ok')
