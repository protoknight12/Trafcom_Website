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

Run as a module from the repo root:  python -m testing.test_offer_generator
"""
import os

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

    os.remove(staged_path)
    db.session.delete(offer2)
    db.session.delete(offer)
    db.session.delete(admin)
    db.session.commit()

print("ok")
