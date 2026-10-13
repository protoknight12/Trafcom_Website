"""
pytest: _bump_stock() increments a persisted row in the database itself (no
lost update when two requests move the same row), and the delete routes refuse
a row something still points at / remove files only after the commit.

A real two-connection race can't be run here: SQLite has no row locks and the
tests share one connection. test_bump_ignores_a_stale_in_memory_value is the
deterministic stand-in - it changes the row behind the ORM's back (what another
request's commit looks like to this session) and checks the bump lands on top.

Run with:
    pytest testing/test_stock_and_delete_integrity.py -v
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked while SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app import (app as flask_app, db, User, Client, MaterialPrice, Detail, DetailDxfFile, Order, OrderItem,
                 Offer, OfferItem, limiter, seed_material_types, _bump_stock)


def _material(key='steel', **extra):
    return MaterialPrice(key=key, display_name=f'Стомана {key}', cost_per_m2=20.0, cutting_speed_mm_per_min=2000,
                         pierce_rate_per_min=30, type='sheets', **extra)


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setitem(flask_app.config, 'DETAIL_DXF_FOLDER', str(tmp_path))
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        db.session.add(User(username='qa_admin', password=generate_password_hash('irrelevant123'), role='admin'))
        db.session.add(_material(stock_quantity=10))
        db.session.commit()
        c = flask_app.test_client()
        c.post('/login', data={'username': 'qa_admin', 'password': 'irrelevant123'})
        yield c
        db.session.remove()
        db.drop_all()


def _steel():
    return MaterialPrice.query.filter_by(key='steel').one()


def _detail(tmp_path, name='Планка', **extra):
    """A Detail with one original DXF on disk; returns (detail id, file path)."""
    detail = Detail(name=name, material_key='steel', width=100, height=50, total_length=300, pierce_count=1,
                    calculated_price=1.0, **extra)
    db.session.add(detail)
    db.session.flush()
    path = tmp_path / f'{detail.id}_original.dxf'
    path.write_text('0\nEOF\n')
    db.session.add(DetailDxfFile(detail_id=detail.id, filename=path.name, original_filename='original.dxf'))
    db.session.commit()
    return detail.id, path


def _flashes(client):
    with client.session_transaction() as sess:
        return [message for _category, message in sess.get('_flashes', [])]


# ----------------- _bump_stock -----------------

def test_several_bumps_in_one_transaction_accumulate(admin_client):
    steel = _steel()
    assert _bump_stock(steel, 5) == 15
    assert _bump_stock(steel, -3) == 12
    assert _bump_stock(steel, 2.5) == 14.5
    assert steel.stock_quantity == 14.5  # what a caller's log line / flash reads right after
    db.session.commit()
    db.session.expire_all()
    assert _steel().stock_quantity == 14.5


def test_bump_ignores_a_stale_in_memory_value(admin_client):
    steel = _steel()
    assert steel.stock_quantity == 10  # loaded - this is the number the old code would have added onto
    db.session.execute(text('UPDATE material_price SET stock_quantity = 100 WHERE id = :id'), {'id': steel.id})
    assert _bump_stock(steel, 5) == 105
    db.session.commit()
    db.session.expire_all()
    assert _steel().stock_quantity == 105


def test_bump_on_row_not_flushed_yet(admin_client):
    with db.session.no_autoflush:
        fresh = _material(key='fresh')
        db.session.add(fresh)
        assert _bump_stock(fresh, 4) == 4
        assert _bump_stock(fresh, 1.5) == 5.5
    db.session.commit()
    assert MaterialPrice.query.filter_by(key='fresh').one().stock_quantity == 5.5


def test_bump_on_transient_object(admin_client):
    loose = Detail(name='не е в сесията')
    assert _bump_stock(loose, 3) == 3 and loose.stock_quantity == 3
    assert _bump_stock(loose, -1) == 2
    assert loose not in db.session


def test_pending_price_change_survives_the_bump(admin_client):
    steel = _steel()
    steel.cost_per_m2 = 33.0  # a delivery note re-pricing the lot, not flushed yet
    assert _bump_stock(steel, 5) == 15
    assert steel.cost_per_m2 == 33.0
    db.session.commit()
    db.session.expire_all()
    steel = _steel()
    assert steel.cost_per_m2 == 33.0 and steel.stock_quantity == 15


# ----------------- delete routes -----------------

def test_detail_on_an_order_line_is_not_deleted(admin_client, tmp_path):
    detail_id, path = _detail(tmp_path)
    order = Order(order_number='T-1', user_id=User.query.one().id, customer_name='Клиент')
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, detail_id=detail_id, quantity_ordered=2))
    db.session.commit()

    response = admin_client.post(f'/admin/details/delete/{detail_id}')
    assert response.status_code == 302
    assert any('поръчка' in m and 'не може да бъде изтрит' in m for m in _flashes(admin_client))
    assert db.session.get(Detail, detail_id) is not None
    assert path.exists()


def test_detail_only_on_an_offer_line_is_not_deleted(admin_client, tmp_path):
    detail_id, path = _detail(tmp_path)
    offer = Offer(number='00000000001')
    db.session.add(offer)
    db.session.flush()
    db.session.add(OfferItem(offer_id=offer.id, item_type='detail', detail_id=detail_id, name='Планка'))
    db.session.commit()

    response = admin_client.post(f'/admin/details/delete/{detail_id}')
    assert response.status_code == 302
    assert any('оферта' in m and 'не може да бъде изтрит' in m for m in _flashes(admin_client))
    assert db.session.get(Detail, detail_id) is not None
    assert path.exists()


def test_unreferenced_detail_is_deleted_with_its_file(admin_client, tmp_path):
    detail_id, path = _detail(tmp_path)
    response = admin_client.post(f'/admin/details/delete/{detail_id}')
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(Detail, detail_id) is None and DetailDxfFile.query.count() == 0
    assert not path.exists()


def test_file_that_will_not_go_does_not_fail_the_delete(admin_client, tmp_path, monkeypatch):
    detail_id, path = _detail(tmp_path)

    def locked(_path):
        raise PermissionError('file in use')

    monkeypatch.setattr(os, 'remove', locked)
    response = admin_client.post(f'/admin/details/delete/{detail_id}')
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(Detail, detail_id) is None  # the delete itself went through
    assert path.exists()


def test_client_owning_a_private_detail_is_not_deleted(admin_client, tmp_path):
    client = Client(name='Фирма ООД')
    db.session.add(client)
    db.session.flush()
    client_id = client.id
    _detail(tmp_path, owner_client_id=client_id)

    response = admin_client.post(f'/admin/clients/{client_id}/delete')
    assert response.status_code == 302
    assert any('собствени детайли' in m and 'не може да бъде изтрит' in m for m in _flashes(admin_client))
    assert db.session.get(Client, client_id) is not None


def test_client_with_nothing_attached_is_deleted(admin_client):
    client = Client(name='Празен клиент')
    db.session.add(client)
    db.session.commit()
    client_id = client.id
    admin_client.post(f'/admin/clients/{client_id}/delete')
    db.session.expire_all()
    assert db.session.get(Client, client_id) is None
