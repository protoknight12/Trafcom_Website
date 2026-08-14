"""
Plain-assert regression coverage for the offer generator (see app.py's
Offer/OfferItem models, sanitize_rich_text(), _resolve_staged_offer_image(),
and the admin_offer_print browser-print view):

- sanitize_rich_text() only allows bold/italic/line-breaks through - guards
  the stored-XSS risk of persisting raw contenteditable HTML.
- _next_offer_number() formats as zero-padded 11 digits starting at 200.
- _resolve_staged_offer_image() only accepts a filename that was actually
  staged in OFFER_IMAGES_FOLDER, same re-check-don't-trust rule as
  _link_order_item_attachment() for PDFs - guards against a crafted
  items_json pointing at an arbitrary path.
- admin_offer_print renders the offer number, a product row's photo, and
  bold/italic markup unescaped (since it's already sanitized - see
  sanitize_rich_text) via the Flask test client.
- _save_offer(), via POST /admin/offers/new: a free-typed ('text') row can
  carry its own name/code/qty/price like a catalog row (not just a
  description paragraph), a note-only row with no qty/price still saves with
  those fields None, and a fully blank row is dropped as junk. valid_until
  parses into a real date and is rejected (not silently dropped) when
  malformed.

Run as a module from the repo root:  python -m testing.test_offer_generator
"""
import json
import os
from datetime import date

from app import app, db, sanitize_rich_text, _next_offer_number, _resolve_staged_offer_image, \
    Offer, OfferItem, User
from werkzeug.security import generate_password_hash

# --- sanitize_rich_text: allowlist only bold/italic/br ---
assert sanitize_rich_text('') is None
assert sanitize_rich_text('   ') is None
assert sanitize_rich_text('plain text') == 'plain text'
assert sanitize_rich_text('<b>bold</b> and <i>italic</i>') == '<b>bold</b> and <i>italic</i>'
assert sanitize_rich_text('<strong>x</strong>') == '<strong>x</strong>'
assert sanitize_rich_text('<script>alert(1)</script>text') == 'text'
assert sanitize_rich_text('<img src=x onerror=alert(1)>text') == 'text'
assert sanitize_rich_text('<div>line1</div><div>line2</div>') == 'line1<br>line2'

with app.app_context():
    OfferItem.query.delete()
    Offer.query.delete()
    db.session.commit()

    # --- _next_offer_number: zero-padded 11 digits, starting at 200 ---
    first = _next_offer_number()
    assert first == '00000000200', first

    offer = Offer(number=first)
    db.session.add(offer)
    db.session.commit()

    second = _next_offer_number()
    assert second == '00000000201', second

    # --- _resolve_staged_offer_image: only a filename actually on disk ---
    image_folder = app.config['OFFER_IMAGES_FOLDER']
    staged_filename = 'test_offer_generator_photo.png'
    staged_path = os.path.join(image_folder, staged_filename)
    with open(staged_path, 'wb') as f:
        f.write(b'not a real image, just needs to exist on disk')

    assert _resolve_staged_offer_image(staged_filename) == staged_filename
    assert _resolve_staged_offer_image('../../etc/passwd') is None
    assert _resolve_staged_offer_image('not_a_real_file.png') is None
    assert _resolve_staged_offer_image('') is None
    assert _resolve_staged_offer_image(None) is None

    # --- admin_offer_print: renders number, photo, and safe rich text ---
    offer2 = Offer(number=second, object_title='Тест обект', footer_notes='Ред 1\nРед 2', signed_by='Иван Иванов')
    db.session.add(offer2)
    db.session.flush()
    db.session.add(OfferItem(offer_id=offer2.id, position=0, item_type='product', code='1.1-M01',
                              name='Тестов продукт', quantity=2, unit='бр', unit_price=10.0,
                              image_filename=staged_filename))
    db.session.add(OfferItem(offer_id=offer2.id, position=1, item_type='text',
                              description_html='<b>Забележка</b>: свободен текст ред'))
    db.session.commit()

    admin = User(username='__test_offer_generator_admin__', password=generate_password_hash('x'), role='admin')
    db.session.add(admin)
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    resp = client.get(f'/admin/offers/{offer2.id}/print')
    assert resp.status_code == 200
    body = resp.data.decode('utf-8')
    assert f'ОФЕРТА № {second}' in body
    assert f'uploads/offers/{staged_filename}' in body
    assert '<b>Забележка</b>' in body  # rendered unescaped, not &lt;b&gt;

    # --- free-typed 'text' rows: standard fields, not just a description ---
    new_resp = client.get('/admin/offers/new')
    csrf = new_resp.data.decode('utf-8').split('name="csrf_token" value="')[1].split('"')[0]
    items = [
        # Priced free-typed line (e.g. "Transport", 1 x 50 EUR) - no catalog link.
        {'type': 'text', 'name': 'Транспорт', 'quantity': 1, 'unit': 'бр', 'unit_price': 50.0},
        # Pure note - only a code + description, no qty/price.
        {'type': 'text', 'code': 'NOTE-1', 'description_html': '<i>само бележка</i>'},
        # Fully blank row - must be dropped, not saved.
        {'type': 'text', 'name': '', 'code': '', 'description_html': ''},
    ]
    post_resp = client.post('/admin/offers/new', data={
        'csrf_token': csrf, 'object_title': '', 'client_id': '', 'signed_by': '',
        'footer_notes': '', 'valid_until': '2026-09-30', 'items_json': json.dumps(items),
    }, follow_redirects=True)
    assert post_resp.status_code == 200

    third_offer = Offer.query.filter(Offer.number.notin_([first, second])).order_by(Offer.id.desc()).first()
    saved_items = sorted(third_offer.items, key=lambda i: i.position)
    assert len(saved_items) == 2, [(i.name, i.code) for i in saved_items]  # the blank row was dropped

    assert saved_items[0].name == 'Транспорт'
    assert saved_items[0].quantity == 1
    assert saved_items[0].unit_price == 50.0
    assert saved_items[0].line_total == 50.0

    assert saved_items[1].code == 'NOTE-1'
    assert saved_items[1].description_html == '<i>само бележка</i>'
    assert saved_items[1].quantity is None
    assert saved_items[1].unit_price is None
    assert saved_items[1].line_total == 0.0

    assert third_offer.total == 50.0

    # --- valid_until: parses into a real date, and renders on the print page ---
    assert third_offer.valid_until == date(2026, 9, 30)
    print_resp = client.get(f'/admin/offers/{third_offer.id}/print')
    assert 'Валидна до: 30.09.2026' in print_resp.data.decode('utf-8')

    # A malformed date must be rejected, not silently dropped or crash.
    edit_resp = client.get(f'/admin/offers/{third_offer.id}/edit')
    edit_csrf = edit_resp.data.decode('utf-8').split('name="csrf_token" value="')[1].split('"')[0]
    bad_resp = client.post(f'/admin/offers/{third_offer.id}/edit', data={
        'csrf_token': edit_csrf, 'object_title': '', 'client_id': '', 'signed_by': '',
        'footer_notes': '', 'valid_until': 'not-a-date', 'items_json': json.dumps(items),
    })
    assert bad_resp.status_code in (302, 200)
    db.session.refresh(third_offer)
    assert third_offer.valid_until == date(2026, 9, 30)  # unchanged, not corrupted

    # --- discount_percent: subtotal/discount_amount/total math, and the
    # "price; -sale%, actual price" print breakdown ---
    discount_resp = client.post(f'/admin/offers/{third_offer.id}/edit', data={
        'csrf_token': edit_csrf, 'object_title': '', 'client_id': '', 'signed_by': '',
        'footer_notes': '', 'valid_until': '2026-09-30', 'discount_percent': '10',
        'items_json': json.dumps(items),
    }, follow_redirects=True)
    assert discount_resp.status_code == 200
    db.session.refresh(third_offer)
    assert third_offer.subtotal == 50.0
    assert third_offer.discount_amount == 5.0
    assert third_offer.total == 45.0

    discounted_print = client.get(f'/admin/offers/{third_offer.id}/print').data.decode('utf-8')
    assert 'Сума:' in discounted_print and '50.00 EUR' in discounted_print
    assert 'Отстъпка (-10%):' in discounted_print and '-5.00 EUR' in discounted_print
    assert '45.00 EUR' in discounted_print  # ОБЩО row

    # An out-of-range discount must be rejected, not silently clamped.
    out_of_range_resp = client.post(f'/admin/offers/{third_offer.id}/edit', data={
        'csrf_token': edit_csrf, 'object_title': '', 'client_id': '', 'signed_by': '',
        'footer_notes': '', 'discount_percent': '150', 'items_json': json.dumps(items),
    })
    assert out_of_range_resp.status_code in (302, 200)
    db.session.refresh(third_offer)
    assert third_offer.discount_percent == 10.0  # unchanged

    # --- admin_offer_duplicate: copies the offer and every item under a new number ---
    dup_resp = client.post(f'/admin/offers/{third_offer.id}/duplicate', data={'csrf_token': edit_csrf}, follow_redirects=True)
    assert dup_resp.status_code == 200
    duplicate = Offer.query.filter(Offer.number.notin_([first, second, third_offer.number])).order_by(Offer.id.desc()).first()
    assert duplicate.number != third_offer.number
    assert duplicate.object_title == third_offer.object_title
    assert duplicate.discount_percent == third_offer.discount_percent
    assert duplicate.valid_until == third_offer.valid_until
    dup_items = sorted(duplicate.items, key=lambda i: i.position)
    assert [(i.item_type, i.name, i.code, i.quantity, i.unit_price) for i in dup_items] == \
           [(i.item_type, i.name, i.code, i.quantity, i.unit_price) for i in sorted(third_offer.items, key=lambda i: i.position)]
    assert duplicate.id != third_offer.id and all(di.id != oi.id for di, oi in zip(dup_items, sorted(third_offer.items, key=lambda i: i.position)))

    os.remove(staged_path)
    db.session.delete(offer2)
    db.session.delete(offer)
    db.session.delete(third_offer)
    db.session.delete(duplicate)
    db.session.delete(admin)
    db.session.commit()

print("ok")
