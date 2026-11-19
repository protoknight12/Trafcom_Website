"""
Parts of the composed material name (owner's decision 2026-11-19, compose_material_name()):
material_type.name_singular ("Лист", "Прът" ... - the word a name starts with; seeded for the 4 standard types when
empty, "other" stays empty = its label), material_price.calibrated (NOT NULL, default false) and
material_price.calibration ("h9"). Safe to re-run.

    python -m migration.migrate_add_material_name_parts
"""
from sqlalchemy import text

from app import app, db

SINGULAR = {'sheets': 'Лист', 'rods': 'Прът', 'profiles': 'Профил', 'pipes': 'Тръба'}


def main():
    with app.app_context():
        db.session.execute(text('ALTER TABLE material_type ADD COLUMN IF NOT EXISTS name_singular VARCHAR(60)'))
        db.session.execute(text('ALTER TABLE material_price ADD COLUMN IF NOT EXISTS calibrated BOOLEAN NOT NULL DEFAULT FALSE'))
        db.session.execute(text('ALTER TABLE material_price ADD COLUMN IF NOT EXISTS calibration VARCHAR(30)'))
        for key, word in SINGULAR.items():
            db.session.execute(text('UPDATE material_type SET name_singular = :w WHERE key = :k AND name_singular IS NULL'),
                               {'w': word, 'k': key})
        db.session.commit()
        print('material_type.name_singular, material_price.calibrated/calibration - OK')


if __name__ == '__main__':
    main()
