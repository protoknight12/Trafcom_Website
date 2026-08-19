"""
One-off: relinks existing OfferItem rows (item_type 'product'/'detail') to
their source Product/Detail via the product_id/detail_id columns added by
migrate_add_offer_item_catalog_ids.py - covers offer lines saved before that
migration, which are otherwise permanently unselectable in
admin_offer_create_order() (re-saving the offer doesn't help, since the save
just carries forward whatever id the row already had - see the offer-edit
page's "премахни и добави отново" note for rows this script can't match).

Best-effort by exact name match against Product.name / Detail.name - only
relinks a row when the name matches exactly one catalog row; skips it
(leaves NULL) when there's no match or more than one, rather than guessing
wrong. Safe to run more than once (only touches rows still missing both ids).

    python -m migration.backfill_offer_item_catalog_ids
"""
from app import app, db, OfferItem, Product, Detail

with app.app_context():
    matched = 0
    ambiguous = 0
    unmatched = 0

    for item in OfferItem.query.filter_by(item_type='product', product_id=None, detail_id=None).all():
        candidates = Product.query.filter_by(name=item.name).all()
        if len(candidates) == 1:
            item.product_id = candidates[0].id
            matched += 1
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            unmatched += 1

    for item in OfferItem.query.filter_by(item_type='detail', product_id=None, detail_id=None).all():
        candidates = Detail.query.filter_by(name=item.name).all()
        if len(candidates) == 1:
            item.detail_id = candidates[0].id
            matched += 1
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            unmatched += 1

    db.session.commit()

print(f"Relinked {matched} offer line(s). {ambiguous} skipped (ambiguous name match), "
      f"{unmatched} skipped (no matching catalog row) - those still need manual removal/re-add.")
