"""
pytest smoke test: every GET route of the app must answer with something below 500.

* test_no_argument_pages_admin   - every GET rule without URL arguments, as an admin.
* test_no_argument_pages_regular - the same sweep as a regular_user (302/403 expected for admin
                                   pages, never 500).
* test_pages_with_one_id         - every GET rule with exactly one <int:...> argument, asked for id 1
                                   of a fresh database in which one row of each main entity (client,
                                   supplier, material, detail, product + recipe, order, offer, delivery
                                   notes, machine, service, production order, ...) was seeded as id 1.
                                   An entity that was not seeded answers 404, which is fine.

Nothing here may contact a device or an external API: socket connects, DNS lookups and subprocesses
are replaced by a recorder that raises, and the test fails if any request tried one.

Run with:
    pytest testing/test_smoke_all_pages.py -v
"""
import atexit
import json
import os
import socket
import subprocess
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup_db_file():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps the file locked as long as SQLAlchemy's pooled connection is open


atexit.register(_cleanup_db_file)

os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

from datetime import date  # noqa: E402

import pytest  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as appmod  # noqa: E402
from app import app as flask_app, db, limiter  # noqa: E402

# endpoint -> why the smoke test does not request it
EXCLUDED = {
    'static': 'static files, not application code',
    'camstream_dev_proxy': 'relays a go2rtc video stream (only registered with CAMSTREAM_PROXY=1)',
    'admin_camera_video': 'proxies a live camera video stream',
    'admin_camera_snapshot': 'fetches a frame from the camera over the network',
    'admin_camera_nvr_channels': 'asks the NVR for its channel list over the network',
}

# endpoint -> reason: GETs that return 500 today because of a real application bug. The suite stays
# green while the entry is here; once the bug is fixed the stale-entry assert fails and the entry must go.
KNOWN_500 = {}

LAST = {'logout'}   # signs the test user out, so it is requested after everything else
ATTEMPTS = []       # (endpoint being requested, what it tried to reach)
_current = ['-']


def _record(what):
    def blocked(*a, **k):
        ATTEMPTS.append((_current[0], what))
        raise OSError(f'network blocked by smoke test: {what}')
    return blocked


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    del ATTEMPTS[:]
    monkeypatch.setattr(socket.socket, 'connect', _record('socket.connect'))
    monkeypatch.setattr(socket.socket, 'connect_ex', _record('socket.connect_ex'))
    monkeypatch.setattr(socket, 'getaddrinfo', _record('getaddrinfo'))
    monkeypatch.setattr(subprocess, 'Popen', _record('subprocess.Popen'))


def _add(*objs):
    db.session.add_all(objs)
    db.session.commit()
    return objs[0] if len(objs) == 1 else objs


def _seed_entities():
    """One row of each main entity; on a fresh database every one of them is id 1."""
    mat = appmod.MaterialPrice.query.first()
    client = _add(appmod.Client(name='Smoke Client'))
    _add(appmod.Deliverer(name='Smoke Courier'))
    supplier = _add(appmod.Supplier(name='Smoke Supplier'))
    machine = _add(appmod.Machine(name='Smoke Machine'))
    _add(appmod.Service(name='Smoke Service', price_per_hour_eur=60))
    shapes = [{'type': 'line', 'x1': 0, 'y1': 0, 'x2': 100, 'y2': 0}, {'type': 'line', 'x1': 100, 'y1': 0, 'x2': 100, 'y2': 50},
              {'type': 'line', 'x1': 100, 'y1': 50, 'x2': 0, 'y2': 50}, {'type': 'line', 'x1': 0, 'y1': 50, 'x2': 0, 'y2': 0}]
    detail = _add(appmod.Detail(name='Smoke Detail', material_key=mat.key, width=100, height=50, total_length=300,
                                pierce_count=1, calculated_price=5, geometry_json=json.dumps(shapes)))
    product = _add(appmod.Product(name='Smoke Product'))
    _add(appmod.ProductDetail(product_id=product.id, detail_id=detail.id, quantity=2))
    order = _add(appmod.Order(order_number='SMOKE-1', user_id=1, customer_name='Smoke Client'))
    _add(appmod.OrderItem(order_id=order.id, product_id=product.id, quantity_ordered=2, unit_price=10),
         appmod.OrderItem(order_id=order.id, detail_id=detail.id, quantity_ordered=1, unit_price=5))
    offer = _add(appmod.Offer(number='SMOKE-OFFER', client_id=client.id, valid_until=date(2030, 1, 1)))
    _add(appmod.OfferItem(offer_id=offer.id, item_type='product', product_id=product.id, name='Smoke Product', quantity=1, unit_price=10),
         appmod.OfferItem(offer_id=offer.id, item_type='text', name='Free text', quantity=1, unit_price=3))
    note = _add(appmod.DeliveryNote(supplier_id=supplier.id, note_number='N-1', note_date=date.today()))
    _add(appmod.DeliveryNoteItem(delivery_note_id=note.id, target_type='material', target_id=mat.id,
                                 description_snapshot=mat.display_name, quantity=1, unit_price=2))
    cnote = _add(appmod.ClientDeliveryNote(client_id=client.id))
    _add(appmod.ClientDeliveryNoteItem(client_delivery_note_id=cnote.id, target_type='detail', target_id=detail.id,
                                       description_snapshot='Smoke Detail', quantity=1))
    _add(appmod.ProductionOrder(detail_id=detail.id, material_id=mat.id, quantity=1, planned_material_qty=1))
    _add(appmod.DxfFile(filename='smoke.dxf', material=mat.key, width=100, height=50, total_length=300, calculated_price=5, user_id=1))
    _add(appmod.DetailDxfFile(detail_id=detail.id, filename='smoke.dxf', original_filename='smoke.dxf'))
    building = _add(appmod.Building(name='Smoke Building'))
    room = _add(appmod.Room(name='Smoke Room', building_id=building.id))
    panel = _add(appmod.ElectricalPanel(name='Smoke Panel', room_id=room.id))
    _add(appmod.PanelComponent(panel_id=panel.id, name='Smoke Breaker'))
    _add(appmod.Convector(name='Smoke Convector'), appmod.TemperatureSensor(name='Smoke Sensor', mqtt_topic='smoke/sensor'),
         appmod.Vehicle(name='Smoke Van', license_plate='CB0000XX'), appmod.HallMachine(name='Smoke Hall Machine'),
         appmod.MeasuringInstrument(name='Smoke Caliper'),
         appmod.RiskRegisterEntry(title='Smoke risk', likelihood=1, impact=1, identified_date=date.today()),
         appmod.ManagementReview(review_date=date.today()))
    cab = _add(appmod.Cabinet(name='Smoke Cabinet'))
    stack = _add(appmod.BatteryStack(cabinet_id=cab.id, name='Smoke Stack'))
    _add(appmod.Battery(stack_id=stack.id), appmod.BatteryModel(name='Smoke Cell', nominal_voltage_v=3.2, capacity_ah=100))
    from migration.seed_bend_tools_m460r import seed   # real punch + die with profiles (same as testing/test_bending_models.py)
    seed()
    _add(appmod.PressBrakeConfig(machine_id=machine.id, tonnage_t=175, working_length_mm=2050))
    detail.thickness_mm = 2
    db.session.commit()
    punch, die = (appmod.BendTool.query.filter_by(code=c).one() for c in ('P.97.75.r08', 'M460.r'))
    prog = _add(appmod.BendProgram(detail_id=detail.id, machine_id=machine.id, name='Smoke Program'))
    _add(appmod.BendStep(program_id=prog.id, seq=1, angle_deg=90, x_mm=40, punch_id=punch.id, die_id=die.id, die_v_mm=16))
    nvr = _add(appmod.CameraNvr(name='Smoke NVR', host='192.0.2.1'))
    _add(appmod.Camera(name='Smoke Camera'))
    _add(appmod.QualityCheck(target_type='detail', target_id=detail.id, inspector_id=1))


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        # the same seeds as app.py's __main__ block, minus seed_shelly_devices() (that one creates polled meters)
        appmod.seed_material_prices()
        appmod.seed_material_types()
        appmod.seed_billable_services()
        appmod.seed_service_machine_cards()
        appmod.seed_index_machine_cards()
        db.session.add_all([
            appmod.User(username='smoke_admin', password=generate_password_hash('irrelevant123'), role='admin'),
            appmod.User(username='smoke_regular', password=generate_password_hash('irrelevant123'), role='regular_user')])
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


def _login(app, username):
    c = app.test_client()
    r = c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    assert r.status_code in (302, 303), f'login as {username} failed: {r.status_code}'
    return c


def _rules(app, predicate, arg=1):
    """endpoint -> concrete URL for every GET rule accepted by predicate(rule), sorted, LAST endpoints at the end."""
    out = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if 'GET' in rule.methods and rule.endpoint not in EXCLUDED and predicate(rule):
            out.setdefault(rule.endpoint, rule.build({a: arg for a in rule.arguments}, append_unknown=False)[1])
    return dict(sorted(out.items(), key=lambda kv: kv[0] in LAST))


def _sweep(client, urls):
    """GET every url; returns {endpoint: (status, detail)}. An exception that Flask would turn into a 500 counts as 500."""
    res = {}
    for ep, url in urls.items():
        _current[0] = ep
        try:
            res[ep] = (client.get(url).status_code, url)
        except Exception as e:  # TESTING=True re-raises instead of answering 500
            db.session.rollback()
            res[ep] = (500, f'{url}  {type(e).__name__}: {str(e)[:300]}')
    return res


def _check(res, *, stale_check):
    assert not ATTEMPTS, f'network/subprocess attempted (exclude the endpoint in EXCLUDED): {ATTEMPTS}'
    bad = {ep: v for ep, v in res.items() if v[0] >= 500 and ep not in KNOWN_500}
    assert not bad, 'GET answered 500:\n' + '\n'.join(f'  {ep}: {u}' for ep, (s, u) in bad.items())
    if stale_check:
        stale = [ep for ep in KNOWN_500 if ep in res and res[ep][0] < 500]
        assert not stale, f'KNOWN_500 entries that no longer fail - remove them: {stale}'


def test_no_argument_pages_admin(app):
    urls = _rules(app, lambda r: not r.arguments)
    assert len(urls) > 90
    res = _sweep(_login(app, 'smoke_admin'), urls)
    print(f'\nno-argument GET pages as admin: {len(urls)}; non-200: ' +
          ', '.join(f'{ep}={s}' for ep, (s, _) in res.items() if s != 200))
    _check(res, stale_check=True)


def test_no_argument_pages_regular_user(app):
    urls = _rules(app, lambda r: not r.arguments)
    res = _sweep(_login(app, 'smoke_regular'), urls)
    _check(res, stale_check=False)


def test_pages_with_one_id(app):
    _seed_entities()
    urls = _rules(app, lambda r: len(r.arguments) == 1 and all(c.__class__.__name__ == 'IntegerConverter'
                                                                for c in r._converters.values()))
    assert len(urls) > 60
    res = _sweep(_login(app, 'smoke_admin'), urls)
    print(f'\none-id GET pages as admin: {len(urls)}; non-200: ' +
          ', '.join(f'{ep}={s}' for ep, (s, _) in res.items() if s != 200))
    _check(res, stale_check=True)


def test_pages_with_unknown_id(app):
    """An id that does not exist must be a 404 (or a redirect), never a crash."""
    urls = _rules(app, lambda r: len(r.arguments) == 1 and all(c.__class__.__name__ == 'IntegerConverter'
                                                                for c in r._converters.values()), arg=999999)
    _check(_sweep(_login(app, 'smoke_admin'), urls), stale_check=True)
