"""
One-time data step (run by migration/run_once.py), owner's decision 2026-11-19: a material's name is composed from its
fields (compose_material_name(): Тип · Калиброван · Вид · Марка · Размери · Калибрация · Забележка).

1. Every MaterialPrice WITH a Вид (grade_id) gets display_name = compose_material_name(lot); a remnant lot (name ending
   in " - остатък") keeps that suffix after the composed name. A lot without a Вид is left alone and listed - its old
   name may carry the only hint of what it is (set its Вид on /admin/materials, then "↻ по полетата" in its dialog).
2. The stored name copies of the RENAMED lots are rewritten to the new name: DeliveryNoteItem / ClientDeliveryNoteItem
   description_snapshot with target_type 'material'. (OrderItem.item_name's "[material]" suffix is computed live from
   OrderItem.material_key, nothing stored to rewrite.) The owner accepts that issued documents then print the new
   names; deploy/update.sh takes a pg_dump first.

Idempotent: a second run composes the same names and changes nothing.

    python -m migration.data_compose_material_names
"""
from app import (app, db, MaterialPrice, DeliveryNoteItem, ClientDeliveryNoteItem, compose_material_name,
                 MATERIAL_NAME_MAX, REMNANT_SUFFIX)


def main():
    """Returns (renamed lots, rewritten snapshots, [names of lots without a Вид])."""
    with app.app_context():
        renamed, no_grade = {}, []
        for lot in MaterialPrice.query.order_by(MaterialPrice.id):
            if not lot.grade_id:
                no_grade.append(f'#{lot.id} {lot.display_name}')
                continue
            name = compose_material_name(lot)
            if lot.display_name.endswith(REMNANT_SUFFIX):
                name = name[:MATERIAL_NAME_MAX - len(REMNANT_SUFFIX)] + REMNANT_SUFFIX
            if name != lot.display_name:
                lot.display_name = renamed[lot.id] = name
        snapshots = 0
        if renamed:
            for model in (DeliveryNoteItem, ClientDeliveryNoteItem):
                for item in model.query.filter(model.target_type == 'material', model.target_id.in_(list(renamed))):
                    if item.description_snapshot != renamed[item.target_id]:
                        item.description_snapshot = renamed[item.target_id]
                        snapshots += 1
        db.session.commit()
    print(f'material names composed: {len(renamed)} lots renamed, {snapshots} delivery/client note lines rewritten')
    if no_grade:
        print(f'left alone (no Вид): {len(no_grade)} - ' + '; '.join(no_grade))
    return len(renamed), snapshots, no_grade


if __name__ == '__main__':
    main()
