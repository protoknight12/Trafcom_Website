"""
One-off test-data seeder: 5 sample Details + 5 sample Products (each built
from 2-3 of those Details), for UI testing. Skips the DXF upload entirely -
geometry numbers are fabricated, not from a real drawing, and geometry_json
is left empty so the dashboard's shape preview has nothing to draw for
these (fine for testing forms/tables/labels, not for the DXF viewer).
Reuses whatever MaterialPrice already exists rather than requiring a
specific one. Safe to run more than once - skips any name that already
exists.

    "D:/python_3131_interpreter/Scripts/python.exe" seed_test_data.py
"""
from app import app, db, Detail, Product, ProductDetail, MaterialPrice

# name, width_mm, height_mm, total_length_mm, pierce_count, price_eur, erp_number, code_number
SAMPLE_DETAILS = [
    ("Тестова плоча А", 300.0, 200.0, 1800.0, 4, 12.50, 9000001, "TEST.01.01"),
    ("Тестова планка Б", 150.0, 80.0, 620.0, 2, 4.20, 9000002, "TEST.01.02"),
    ("Тестов фланец В", 220.0, 220.0, 2100.0, 6, 18.90, 9000003, "TEST.01.03"),
    ("Тестова скоба Г", 90.0, 45.0, 340.0, 1, 2.10, 9000004, "TEST.01.04"),
    ("Тестов панел Д", 500.0, 350.0, 3200.0, 8, 34.75, 9000005, "TEST.01.05"),
]

# name, description, markup_percent, erp_number, code_number, [(detail_index, quantity), ...]
SAMPLE_PRODUCTS = [
    ("Тестов продукт 1", "Примерен продукт за тестове", 15.0, 9001001, "TESTP.01", [(0, 2), (1, 4)]),
    ("Тестов продукт 2", "Примерен продукт за тестове", 20.0, 9001002, "TESTP.02", [(2, 1)]),
    ("Тестов продукт 3", "Примерен продукт за тестове", 10.0, 9001003, "TESTP.03", [(3, 6), (4, 1)]),
    ("Тестов продукт 4", "Примерен продукт за тестове", 25.0, 9001004, "TESTP.04", [(0, 1), (2, 1), (4, 1)]),
    ("Тестов продукт 5", "Примерен продукт за тестове", 5.0, 9001005, "TESTP.05", [(1, 10)]),
]

with app.app_context():
    material = MaterialPrice.query.first()
    if not material:
        raise SystemExit("No MaterialPrice rows found - add a material first, then re-run this.")

    details = []
    for name, width, height, length, pierces, price, erp, code in SAMPLE_DETAILS:
        existing = Detail.query.filter_by(name=name).first()
        if existing:
            details.append(existing)
            continue
        d = Detail(
            name=name, material_key=material.key, width=width, height=height,
            total_length=length, pierce_count=pierces, calculated_price=price,
            erp_number=erp, code_number=code
        )
        db.session.add(d)
        db.session.flush()  # assigns d.id for the ProductDetail rows below
        details.append(d)

    for name, description, markup, erp, code, recipe in SAMPLE_PRODUCTS:
        if Product.query.filter_by(name=name).first():
            continue
        p = Product(name=name, description=description, markup_percent=markup,
                    erp_number=erp, code_number=code)
        db.session.add(p)
        db.session.flush()
        for detail_index, quantity in recipe:
            db.session.add(ProductDetail(product_id=p.id, detail_id=details[detail_index].id, quantity=quantity))

    db.session.commit()

print("Seeded 5 test Details and 5 test Products (or skipped ones that already existed).")
