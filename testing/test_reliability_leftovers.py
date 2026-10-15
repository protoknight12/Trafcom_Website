"""
pytest for four reliability leftovers - no device and no PostgreSQL is ever contacted, every failure is a fake:

    _handle_mqtt_message()   topic -> device lookup is cached: N messages cost O(1) queries; a device added (or removed)
                             through the ORM is picked up from the next message
    _detection_lock()        a dead advisory-lock connection is dropped and the lock re-taken on a fresh one, logged once
                             per failure episode; the detector does not run while it has no lock
    _heatpump_loop()         a DB error on the minute's HeatPumpReading is rolled back and logged as 'heatpump-db' and
                             does NOT drop the pump connection; a pump error still does
    retry_on_db_busy         a deadlock on commit -> the whole handler runs again, stock moves exactly once, flashes once;
                             two deadlocks -> "system busy" flash, nothing moved

Run with:
    python -m pytest testing/test_reliability_leftovers.py -q -p no:cacheprovider
"""
import atexit
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)


def _cleanup():
    try:
        os.remove(_db_path)
    except OSError:
        pass  # Windows keeps it locked while SQLAlchemy's pool is open


atexit.register(_cleanup)
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

import app as appmod
from app import (app as flask_app, db, limiter, User, ShellyDevice, TemperatureSensor, Convector, MaterialPrice, Detail,
                 ProductionOrder, HeatPumpReading)


@pytest.fixture(autouse=True)
def database():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
    yield
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


class _Deadlock(Exception):
    pgcode = '40P01'   # what psycopg2's DeadlockDetected carries from the server


def _deadlock():
    return OperationalError('COMMIT', {}, _Deadlock('deadlock detected'))


# ----------------- 1. MQTT topic cache -----------------

def _count_selects():
    seen = []

    def on_exec(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            seen.append(statement)

    with flask_app.app_context():
        engine = db.engine
    event.listen(engine, 'before_cursor_execute', on_exec)
    return seen, lambda: event.remove(engine, 'before_cursor_execute', on_exec)


def test_mqtt_messages_cost_o1_queries_and_new_device_is_picked_up():
    appmod._mqtt_state.clear()
    appmod._mqtt_temp_state.clear()
    with flask_app.app_context():
        db.session.add_all([ShellyDevice(name='Мер 1', mqtt_topic='shellies/em1'),
                            TemperatureSensor(name='Т1', mqtt_topic='shellies/ht1', sensor_type='shelly_ht'),
                            Convector(name='К1', connection_type='mqtt', mqtt_topic='shellies/conv1')])
        db.session.commit()
    appmod._mqtt_topics['rows'] = None   # whatever an earlier test file left

    seen, stop = _count_selects()
    try:
        for i in range(50):
            appmod._handle_mqtt_message('shellies/em1/emeter/0/power', str(100 + i))
            appmod._handle_mqtt_message('shellies/ht1/sensor/temperature', '21.5')
            appmod._handle_mqtt_message('somebody/else/topic', 'x')
        assert len(seen) == 3, seen                      # one load (3 SELECTs) for 150 messages, was 450
        assert appmod._mqtt_state['shellies/em1']['channels'][0]['power'] == 149.0
        assert 'shellies/ht1' in appmod._mqtt_temp_state

        appmod._handle_mqtt_message('shellies/em2/emeter/0/power', '7')
        assert 'shellies/em2' not in appmod._mqtt_state  # unknown yet

        with flask_app.app_context():                   # a device added the way the routes do it (ORM + commit)
            db.session.add(ShellyDevice(name='Мер 2', mqtt_topic='shellies/em2'))
            db.session.commit()
        seen.clear()
        appmod._handle_mqtt_message('shellies/em2/emeter/0/power', '7')
        appmod._handle_mqtt_message('shellies/em2/emeter/0/power', '8')
        assert appmod._mqtt_state['shellies/em2']['channels'][0]['power'] == 8.0
        assert len(seen) == 3                            # reloaded once after the commit, then cached again

        with flask_app.app_context():                   # an unrelated commit does not throw the cache away
            db.session.add(User(username='mqtt_bystander', password='x', role='worker'))
            db.session.commit()
        seen.clear()
        appmod._handle_mqtt_message('shellies/em2/emeter/0/power', '9')
        assert seen == []

        with flask_app.app_context():                   # an edited topic counts too
            db.session.query(ShellyDevice).filter_by(name='Мер 2').one().mqtt_topic = 'shellies/em2b'
            db.session.commit()
        appmod._handle_mqtt_message('shellies/em2b/emeter/0/power', '11')
        assert appmod._mqtt_state['shellies/em2b']['channels'][0]['power'] == 11.0
    finally:
        stop()


def test_mqtt_cache_backstop_age(monkeypatch):
    """A change the session events cannot see (raw SQL / another process) is picked up after MQTT_TOPICS_MAX_AGE."""
    appmod._mqtt_state.clear()
    appmod._mqtt_topics['rows'] = None
    appmod._handle_mqtt_message('raw/em/emeter/0/power', '1')
    with flask_app.app_context():
        db.session.execute(db.text("INSERT INTO shelly_device (name, mqtt_topic, connection_type) VALUES ('raw', 'raw/em', 'mqtt')"))
        db.session.commit()
    appmod._handle_mqtt_message('raw/em/emeter/0/power', '2')
    assert 'raw/em' not in appmod._mqtt_state            # still cached
    monkeypatch.setattr(appmod, 'MQTT_TOPICS_MAX_AGE', 0)
    appmod._handle_mqtt_message('raw/em/emeter/0/power', '3')
    assert appmod._mqtt_state['raw/em']['channels'][0]['power'] == 3.0


# ----------------- 2. detector advisory lock -----------------

class _FakeConn:
    def __init__(self, dead=False):
        self.dead, self.closed, self.sql = dead, False, []

    def execute(self, stmt):
        if self.dead:
            raise OperationalError(str(stmt), {}, Exception('server closed the connection unexpectedly'))
        self.sql.append(str(stmt))
        return type('R', (), {'scalar': lambda _self: 1})()

    def commit(self):
        pass

    def close(self):
        self.closed = True


def test_detection_lock_reconnects_after_a_dead_connection(monkeypatch):
    with flask_app.app_context():
        engine = db.engine
    monkeypatch.setattr(engine.dialect, 'name', 'postgresql')
    for name, value in (('_detect_lock_conn', None), ('_detect_lock_broken', False), ('_detect_lock_checked', 0.0)):
        monkeypatch.setattr(appmod, name, value)
    conns = [_FakeConn()]
    connect = {'fail': False}

    def fake_connect():
        if connect['fail']:
            raise OperationalError('connect', {}, Exception('could not connect to server'))
        return conns[-1]

    monkeypatch.setattr(engine, 'connect', fake_connect)
    logged = []
    monkeypatch.setattr(appmod.app.logger, 'exception', lambda *a, **k: logged.append(a))

    with flask_app.app_context():
        assert appmod._detection_lock(False) is True                 # taken
        assert 'pg_try_advisory_lock' in conns[0].sql[-1]
        n = len(conns[0].sql)
        assert appmod._detection_lock(True) is True                  # held, checked recently: no query
        assert len(conns[0].sql) == n

        monkeypatch.setattr(appmod, 'DETECT_LOCK_CHECK_EVERY', 0)
        assert appmod._detection_lock(True) is True                  # check: still held on a live connection
        assert 'pg_locks' in conns[0].sql[-1]

        conns[0].dead = True                                         # DB restart
        assert appmod._detection_lock(True) is False
        assert conns[0].closed and appmod._detect_lock_conn is None
        connect['fail'] = True                                       # DB still down
        assert appmod._detection_lock(False) is False
        assert appmod._detection_lock(False) is False
        assert len(logged) == 1                                      # one log line for the whole episode

        connect['fail'] = False
        conns.append(_FakeConn())
        assert appmod._detection_lock(False) is True                 # re-taken on a fresh connection
        assert appmod._detect_lock_conn is conns[1] and 'pg_try_advisory_lock' in conns[1].sql[-1]
        conns[1].dead = True
        assert appmod._detection_lock(True) is False
        assert len(logged) == 2                                      # a new episode logs again


def test_detector_does_not_run_after_losing_the_lock(monkeypatch):
    with flask_app.app_context():
        engine = db.engine
    monkeypatch.setattr(engine.dialect, 'name', 'postgresql')
    monkeypatch.setattr(appmod, 'DETECT_LOCK_CHECK_EVERY', 0)
    monkeypatch.setattr(appmod, '_detect_lock_conn', _FakeConn(dead=True))
    monkeypatch.setattr(appmod, '_detect_lock_broken', True)         # already logged
    monkeypatch.setattr(engine, 'connect', lambda: _FakeConn(dead=True))
    ticks = []
    monkeypatch.setattr(appmod, '_detection_tick', lambda memo: ticks.append(1))
    st = {'has_lock': True, 'last_clean': 0}
    appmod._detection_loop_once({}, st)
    assert st['has_lock'] is False and ticks == []


# ----------------- 3. heat pump: DB errors vs pump errors -----------------

class _StopLoop(BaseException):
    pass


def _run_heatpump(monkeypatch, ticks, fast=None, commit_failures=0):
    opened, closed = [], []
    monkeypatch.setattr(appmod, 'get_text', lambda key, default='': '10.0.0.9:23')
    monkeypatch.setattr(appmod, '_heatpump_open', lambda host: opened.append(host) or object())
    monkeypatch.setattr(appmod, '_heatpump_close', lambda hp: closed.append(hp))
    monkeypatch.setattr(appmod, '_heatpump_fast', fast or (lambda hp: {'Temp. Aussen': 5.0}))
    monkeypatch.setattr(appmod, '_heatpump_slow', lambda hp: {'Betriebsart': 1})
    monkeypatch.setattr(appmod, 'HEATPUMP_SLOW_INTERVAL', 0)
    failed = []
    real_failed = appmod._poller_failed
    monkeypatch.setattr(appmod, '_poller_failed', lambda name: failed.append(name) or real_failed(name))

    real_commit, calls = db.session.commit, {'n': 0}

    def flaky_commit():
        calls['n'] += 1
        if calls['n'] <= commit_failures:
            raise OperationalError('INSERT', {}, Exception('database is restarting'))
        return real_commit()

    monkeypatch.setattr(db.session, 'commit', flaky_commit)
    slept = []

    def fake_sleep(s):
        slept.append(s)
        if len(slept) >= ticks:
            raise _StopLoop

    monkeypatch.setattr(appmod.time, 'sleep', fake_sleep)
    appmod._poller_seen.pop('heatpump', None)
    with pytest.raises(_StopLoop):
        appmod._heatpump_loop()
    with flask_app.app_context():
        rows = HeatPumpReading.query.count()
    return opened, closed, failed, rows


def test_heatpump_db_error_keeps_the_pump_connection(monkeypatch):
    opened, closed, failed, rows = _run_heatpump(monkeypatch, ticks=3, commit_failures=1)
    assert opened == ['10.0.0.9:23'] and closed == []                # one session the whole time
    assert failed == ['heatpump-db']                                 # logged in its own (rate-limited) domain
    assert rows == 2                                                 # tick 1's row lost, ticks 2 and 3 written
    assert appmod._heatpump_live['online'] is True and appmod._heatpump_live['error'] is None
    assert 'heatpump' in appmod._poller_seen


def test_heatpump_pump_error_still_reconnects(monkeypatch):
    calls = {'n': 0}

    def fast(hp):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('converter closed the socket')
        return {'Temp. Aussen': 5.0}

    opened, closed, failed, rows = _run_heatpump(monkeypatch, ticks=2, fast=fast)
    assert len(opened) == 2 and len(closed) == 1                     # dropped and re-opened, as before
    assert failed == ['heatpump'] and rows == 1


# ----------------- 4. deadlock retry on stock-moving routes -----------------

@pytest.fixture
def client():
    with flask_app.app_context():
        db.session.add(User(username='rl_worker', password=generate_password_hash('irrelevant123'), role='worker'))
        material = MaterialPrice(key='rl_sheet', display_name='RL Ламарина', cost_per_m2=10.0, cutting_speed_mm_per_min=1000,
                                 pierce_rate_per_min=30, type='sheets', stock_quantity=1.0)
        db.session.add(material)
        db.session.flush()
        db.session.add(Detail(name='RL Detail', material_key=material.key, width=1000.0, height=500.0,
                              total_length=0.0, pierce_count=0, calculated_price=5.0))
        db.session.commit()
    c = flask_app.test_client()
    c.post('/login', data={'username': 'rl_worker', 'password': 'irrelevant123'})
    return c


def _job(status='pending'):
    with flask_app.app_context():
        d = Detail.query.filter_by(name='RL Detail').first()
        m = MaterialPrice.query.filter_by(key='rl_sheet').first()
        job = ProductionOrder(detail_id=d.id, material_id=m.id, quantity=1, planned_material_qty=0.5)
        if status == 'done':
            job.status, job.actual_material_qty = 'done', 0.5
            m.stock_quantity, d.stock_quantity = 0.5, 1
        db.session.add(job)
        db.session.commit()
        return job.id


def _state(job_id):
    with flask_app.app_context():
        job = db.session.get(ProductionOrder, job_id)
        return (job.status, job.reversed_at is not None, MaterialPrice.query.filter_by(key='rl_sheet').first().stock_quantity,
                Detail.query.filter_by(name='RL Detail').first().stock_quantity or 0)


def _deadlocking_commit(monkeypatch, failures):
    real_commit, calls = db.session.commit, {'n': 0}

    def commit():
        calls['n'] += 1
        if calls['n'] <= failures:
            raise _deadlock()
        return real_commit()

    monkeypatch.setattr(db.session, 'commit', commit)
    return calls


def test_complete_job_retries_once_after_a_deadlock(client, monkeypatch):
    jid = _job()
    calls = _deadlocking_commit(monkeypatch, failures=1)
    res = client.post(f'/admin/production-orders/{jid}/complete', data={'actual_material_qty': 0.5}, follow_redirects=True)
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert body.count('наличностите са обновени') == 1
    assert _state(jid) == ('done', False, 0.5, 1)                    # moved exactly once, not twice
    assert calls['n'] >= 2


def test_flash_from_the_failed_attempt_is_not_shown_twice(client, monkeypatch):
    jid = _job('done')
    _deadlocking_commit(monkeypatch, failures=1)                    # delete_production_order() flashes BEFORE its commit
    res = client.post(f'/admin/production-orders/{jid}/delete', follow_redirects=True)
    assert res.get_data(as_text=True).count('Завършената задача беше изтрита') == 1
    assert _state(jid) == ('done', True, 1.0, 0)


def test_two_deadlocks_give_the_busy_message_and_nothing_moves(client, monkeypatch):
    jid = _job()
    _deadlocking_commit(monkeypatch, failures=2)
    res = client.post(f'/admin/production-orders/{jid}/complete', data={'actual_material_qty': 0.5},
                      headers={'Referer': '/admin/production-orders'}, follow_redirects=True)
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert 'Системата беше заета' in body and 'наличностите са обновени' not in body
    assert _state(jid) == ('pending', False, 1.0, 0)


def test_other_db_errors_are_not_retried(client, monkeypatch):
    jid = _job()
    real_commit, calls = db.session.commit, {'n': 0}

    def commit():
        calls['n'] += 1
        raise OperationalError('COMMIT', {}, Exception('disk full'))

    monkeypatch.setattr(db.session, 'commit', commit)
    flask_app.config['PROPAGATE_EXCEPTIONS'] = True
    try:
        with pytest.raises(OperationalError):
            client.post(f'/admin/production-orders/{jid}/complete', data={'actual_material_qty': 0.5})
    finally:
        flask_app.config['PROPAGATE_EXCEPTIONS'] = None
    assert calls['n'] == 1
    monkeypatch.setattr(db.session, 'commit', real_commit)
    with flask_app.app_context():
        db.session.rollback()
    assert _state(jid) == ('pending', False, 1.0, 0)


def test_production_report_ajax_gets_json_when_busy(client, monkeypatch):
    """The production report is fetch()ed: the busy answer there is JSON its JS alerts, not a flash."""
    calls = {'n': 0}

    def view():
        calls['n'] += 1
        raise _deadlock()

    with flask_app.test_request_context('/admin/production', method='POST'):
        res = appmod.retry_on_db_busy(view, json_reply=True)()
    assert calls['n'] == 2
    assert res[1] == 503 and res[0].get_json()['status'] == 'error'
