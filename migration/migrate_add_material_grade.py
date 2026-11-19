"""
"Вид на материала": creates the material_grade table (name, density kg/dm³) with the standard grades
(DEFAULT_MATERIAL_GRADES - steel, stainless, aluminium ...), adds material_price.grade_id and fills it for
every material without one by the keywords in its name (guess_material_grade_id()). The density turns m² / m
into kg (material_kg_per_unit()). Safe to re-run.

    python -m migration.migrate_add_material_grade
"""
from sqlalchemy import text

from app import app, db, MaterialGrade, MaterialPrice, seed_material_grades, guess_material_grade_id


def main():
    with app.app_context():
        MaterialGrade.__table__.create(db.engine, checkfirst=True)
        db.session.execute(text('ALTER TABLE material_price ADD COLUMN IF NOT EXISTS grade_id INTEGER '
                                'REFERENCES material_grade(id)'))
        db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_material_price_grade_id ON material_price (grade_id)'))
        db.session.commit()
        seed_material_grades()
        filled = 0
        for m in MaterialPrice.query.filter(MaterialPrice.grade_id.is_(None)):
            m.grade_id = guess_material_grade_id(m.display_name)
            filled += bool(m.grade_id)
        db.session.commit()
        print(f'material_grade + material_price.grade_id - OK ({filled} materials got a grade from their name)')


if __name__ == '__main__':
    main()
