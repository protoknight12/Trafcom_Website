"""
material_kg_per_unit() / material_stock_kg() - the area/length <-> kg conversion: measured weight_kg wins,
else density (own or guessed from the name) x geometry per type. Run: python -m testing.test_material_weight
"""
import math

from app import (MaterialPrice, MaterialGrade, material_kg_per_unit, material_stock_kg, material_kg_per_stock_unit,
                 material_density, material_computed_kg_per_stock_unit, material_qty_text)


def mat(**kw):
    kw.setdefault('display_name', 'Стомана DC01')
    kw.setdefault('type', 'sheets')
    return MaterialPrice(**kw)


def main():
    sheet = mat(thickness_mm=2, sheet_width_mm=1000, sheet_length_mm=2000, stock_quantity=3)
    assert material_density(sheet) == 7.85
    assert math.isclose(material_kg_per_unit(sheet), 15.7)                 # kg/m²
    assert math.isclose(material_kg_per_stock_unit(sheet), 31.4)           # one 2 m² sheet
    assert math.isclose(material_stock_kg(sheet), 94.2)                    # 3 sheets
    measured = mat(thickness_mm=2, sheet_width_mm=1000, sheet_length_mm=2000, weight_kg=30)
    assert math.isclose(material_kg_per_stock_unit(measured), 30)          # measured sheet weight wins
    assert math.isclose(material_computed_kg_per_stock_unit(measured), 31.4)   # the computed one stays visible
    assert material_density(mat(display_name='Неръждаема стомана AISI 304')) == 7.93
    assert material_density(mat(display_name='Алуминий 5754')) == 2.70
    assert material_density(mat(display_name='Поликарбонат')) is None
    assert material_kg_per_unit(mat(display_name='Поликарбонат', thickness_mm=4)) is None
    assert math.isclose(material_kg_per_unit(mat(display_name='Поликарбонат', thickness_mm=4, grade=MaterialGrade(name='PC', density_kg_dm3=1.2))), 4.8)
    rod = mat(type='rods', sheet_width_mm=20)                                # ⌀20 solid: 2.466 kg/m
    assert math.isclose(material_kg_per_unit(rod), 7.85 * math.pi * 100 / 1000)
    pipe = mat(type='pipes', sheet_width_mm=40, thickness_mm=2)
    assert math.isclose(material_kg_per_unit(pipe), 7.85 * math.pi / 4 * (1600 - 1296) / 1000)
    tube = mat(type='profiles', sheet_width_mm=40, height_mm=20, thickness_mm=2)
    assert math.isclose(material_kg_per_unit(tube), 7.85 * (800 - 36 * 16) / 1000)
    assert material_kg_per_unit(mat(type='rods', sheet_width_mm=20, weight_kg=2.5)) == 2.5
    assert material_kg_per_unit(mat(type='other')) is None                  # no thickness
    assert material_qty_text(mat(type='rods', sheet_width_mm=20, sheet_length_mm=3000, stock_quantity=9)) == '3 бр. = 9 м'
    assert material_qty_text(mat(type='rods', sheet_width_mm=20), 2.5) == '2.5 м'     # no bar length
    assert material_qty_text(sheet) == '3 листа = 6 м²'                     # stock counts sheets
    assert material_qty_text(sheet, 1) == '1 лист = 2 м²'
    assert material_qty_text(mat(thickness_mm=2), 4.5) == '4.5 м²'          # no sheet size: stock in m²
    # JS mirror (static/js/material_weight.js qtyText) gives the same text
    import json, os, shutil, subprocess
    if shutil.which('node'):
        js = os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'material_weight.js')
        out = subprocess.run(['node', '-e', 'const K=require(process.argv[1]);console.log(JSON.stringify(['
                              'K.qtyText(3,{unit:"m2",width:1000,length:2000}),K.qtyText(4.5,{unit:"m2"}),'
                              'K.qtyText(9,{unit:"m",length:3000}),K.qtyText(5,{unit:"pcs"}),K.qtyText(2,null)]))', js],
                             capture_output=True, encoding='utf-8', check=True).stdout
        assert json.loads(out) == ['3 листа = 6 м²', '4.5 м²', '3 бр. = 9 м', '5 бр.', '2 бр.'], out
    print('test_material_weight OK')


if __name__ == '__main__':
    main()
