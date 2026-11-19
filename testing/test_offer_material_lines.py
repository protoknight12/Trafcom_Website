"""
pytest: "Материал" lines in an offer (selling raw material). The line points at one of OUR lots
(OfferItem.material_id), its quantity is in the lot's stock unit (лист / м / бр) and its price is pre-filled from
the server: material_stock_unit_price() = _material_terms(delivery cost of one stock unit, client) - the standard
markup (20%) without client terms, the client's own Материали terms when set. "Обнови" (current_price) works for it,
the print shows unit + kg, an order from the offer skips it and a lot an offer line refers to cannot be deleted.

Run with:
    pytest testing/test_offer_material_lines.py -v
"""
import json

import pytest
from flask import g
from werkzeug.security import generate_password_hash

import app as appmod
from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, Offer, OfferItem, OrderItem, limiter,
                 seed_material_types)


@pytest.fixture
def ids():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        admin = User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin')
        client = Client(name='Клиент М', material_adjustment_type='discount', material_adjustment_percent=10)
        plain = Client(name='Клиент без условия')
        # sheet lot 1000 x 2000 = 2 m² at 10 €/m² -> one sheet costs 20 €; linear lot 5 €/m
        sheet = MaterialPrice(key='steel2', display_name='Стомана 2мм', cost_per_m2=10, type='sheets',
                              sheet_width_mm=1000, sheet_length_mm=2000, thickness_mm=2)
        rod = MaterialPrice(key='rod20', display_name='Прът ф20', cost_per_m2=5, type='rods', sheet_width_mm=20,
                            sheet_length_mm=6000)
        theirs = MaterialPrice(key='their', display_name='Чужд лист', cost_per_m2=10, type='sheets')
        db.session.add_all([admin, client, plain, sheet, rod])
        db.session.flush()
        theirs.owner_client_id = client.id
        db.session.add(theirs)
        db.session.flush()
        detail = Detail(name='Планка', material_key='steel2', width=100, height=100, total_length=1, pierce_count=1,
                        calculated_price=10.0)
        db.session.add(detail)
        db.session.commit()
        yield dict(admin=admin.id, client=client.id, plain=plain.id, sheet=sheet.id, rod=rod.id, theirs=theirs.id,
                   detail=detail.id)
        db.session.remove()
        db.drop_all()


def _login():
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
    return c


def _save(c, ids, rows, client_id=''):
    return c.post('/admin/offers/new', data={'client_id': str(client_id), 'items_json': json.dumps(rows)})


def test_stock_unit_price_standard_and_client_terms(ids):
    with flask_app.app_context():
        sheet, rod = db.session.get(MaterialPrice, ids['sheet']), db.session.get(MaterialPrice, ids['rod'])
        client, plain = db.session.get(Client, ids['client']), db.session.get(Client, ids['plain'])
        assert appmod.material_stock_unit_price(sheet) == 24.0           # 20 € sheet + standard 20%
        assert appmod.material_stock_unit_price(sheet, plain) == 24.0    # no own terms -> standard
        assert appmod.material_stock_unit_price(sheet, client) == 18.0   # 20 € - the client's 10%
        assert appmod.material_stock_unit_price(rod) == 6.0              # 5 €/m + 20%
        assert appmod.material_stock_unit_price(rod, client) == 4.5
        assert appmod.material_stock_unit_label(sheet) == 'лист' and appmod.material_stock_unit_label(rod) == 'м'
        clients = Client.query.all()
        row = appmod._offer_material_json(sheet, clients)
        assert row['price'] == 24.0 and row['client_prices'] == {ids['client']: 18.0} and row['unit'] == 'лист'


def test_editor_lists_our_lots_only_with_server_prices(ids):
    page = _login().get('/admin/offers/new').get_data(as_text=True)
    assert 'value="material"' in page and 'data-picker="material"' in page
    assert 'value="steel2"' in page and 'value="rod20"' in page
    assert 'value="their"' not in page                             # a client's stored lot is not for sale
    assert '"client_prices": {"%d": 18.0}' % ids['client'] in page or '"client_prices":{"%d":18.0}' % ids['client'] in page


def test_save_material_line_and_current_price(ids):
    c = _login()
    rows = [{'type': 'material', 'material_id': ids['sheet'], 'quantity': 3, 'unit_price': 18.0, 'unit': 'x'},
            {'type': 'material', 'material_id': ids['rod'], 'quantity': 12.5, 'unit_price': 4.5},
            {'type': 'material', 'material_id': ids['theirs'], 'quantity': 1, 'unit_price': 1},   # refused: client's lot
            {'type': 'material', 'material_id': ids['sheet'], 'quantity': 0, 'unit_price': 1}]    # refused: no qty
    _save(c, ids, rows, ids['client'])
    with flask_app.app_context():
        offer = Offer.query.one()
        lines = offer.items
        assert [(i.item_type, i.material_id, i.name, i.unit, i.quantity, i.unit_price) for i in lines] == [
            ('material', ids['sheet'], 'Стомана 2мм', 'лист', 3, 18.0),
            ('material', ids['rod'], 'Прът ф20', 'м', 12.5, 4.5)]
        assert offer.total == 110.25
        assert [r['current_price'] for r in appmod._offer_items_json(offer)] == [18.0, 4.5]
        # delivery price moves: the line is stale, "Обнови" offers the new figure
        db.session.get(MaterialPrice, ids['sheet']).cost_per_m2 = 15
        db.session.commit()
        assert appmod._offer_items_json(offer)[0]['current_price'] == 27.0   # 30 € sheet - 10%
        offer.client_id = None
        db.session.commit()
        assert appmod._offer_items_json(offer)[0]['current_price'] == 36.0   # standard 20%


def test_print_shows_unit_and_kg(ids):
    c = _login()
    _save(c, ids, [{'type': 'material', 'material_id': ids['sheet'], 'quantity': 3, 'unit_price': 24.0}])
    with flask_app.app_context():
        offer = Offer.query.one()
        kg = offer.items[0].material_kg
        assert kg and abs(kg - 3 * appmod.material_kg_per_stock_unit(db.session.get(MaterialPrice, ids['sheet']))) < 1e-9
        offer_id = offer.id
    html = c.get(f'/admin/offers/{offer_id}/print').get_data(as_text=True)
    assert 'лист' in html and appmod.kg_text(kg, 2) in html


def test_create_order_skips_material_lines(ids):
    c = _login()
    _save(c, ids, [{'type': 'material', 'material_id': ids['sheet'], 'quantity': 3, 'unit_price': 24.0},
                   {'type': 'detail', 'detail_id': ids['detail'], 'name': 'Планка', 'quantity': 2, 'unit_price': 10.0}])
    with flask_app.app_context():
        offer = Offer.query.one()
        offer_id, item_ids = offer.id, [str(i.id) for i in offer.items]
        material_only = [str(i.id) for i in offer.items if i.item_type == 'material']
    c.post(f'/admin/offers/{offer_id}/create-order', data={'customer_name': 'QA', 'item_ids': material_only})
    with flask_app.app_context():
        assert OrderItem.query.count() == 0
    c.post(f'/admin/offers/{offer_id}/create-order', data={'customer_name': 'QA', 'item_ids': item_ids})
    with flask_app.app_context():
        (line,) = OrderItem.query.all()
        assert line.detail_id == ids['detail']


def test_delete_material_refused_while_an_offer_line_uses_it(ids):
    c = _login()
    _save(c, ids, [{'type': 'material', 'material_id': ids['rod'], 'quantity': 1, 'unit_price': 6.0}])
    res = c.post('/admin/materials/rod20/delete', follow_redirects=True)
    assert 'оферта' in res.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(MaterialPrice, ids['rod']) is not None
        db.session.delete(Offer.query.one())
        db.session.commit()
    c.post('/admin/materials/rod20/delete')
    with flask_app.app_context():
        assert db.session.get(MaterialPrice, ids['rod']) is None
