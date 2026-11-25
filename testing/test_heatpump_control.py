"""pytest: heat pump control (owner's request 2026-11-25) - thermostat gate, hard restart, automatic restart on chosen
faults, fault-list sync, the pump's weekly schedule, freeze / overheat protection, the page's tabs.
Fake devices only (relay, pump writes, sleeps), never a real wait.

    python -m pytest testing/test_heatpump_control.py -q
"""
import datetime
import json
import time

import pytest

import app as app_module
import hvac
from app import (app, db, EditableText, HeatPumpFault, HeatPumpInterval, ActivityLog, Convector, HeatingHoliday)
from testing.test_thermostats import env, _season, _thermo, _login  # noqa: F401  (env is a fixture)

NOW = datetime.datetime(2026, 11, 23, 10, 0)     # Monday, local naive
HP = 'settings.heatpump_'


@pytest.fixture
def hp(env, monkeypatch):
    """env (thermostats + fake convectors) plus clean pump state: live offline, no restart, no settings, fake writes/relay."""
    for m in (HeatPumpFault, HeatPumpInterval):
        m.query.delete()
    EditableText.query.filter(EditableText.key.like(HP + '%')).delete(synchronize_session=False)
    db.session.commit()
    monkeypatch.setattr(app_module, '_heatpump_live', {'ts': None, 'online': False, 'error': None, 'data': {}, 'slow': {}, 'slow_ts': None})
    monkeypatch.setattr(app_module, '_heatpump_restart', {'running': False, 'step': None, 'started': None, 'finished': None,
                                                          'ok': None, 'error': None, 'reason': None, 'mode': None})
    monkeypatch.setattr(app_module, '_heatpump_relay_cache', {'is_on': None})
    monkeypatch.setattr(app_module, '_heatpump_auto', {'trigger': None, 'times': [], 'limit_logged': False})
    monkeypatch.setattr(app_module, '_hvac_protect', {'reason': None, 'cfg': {}})
    env['writes'] = []
    monkeypatch.setattr(app_module, '_heatpump_write', lambda n, v: env['writes'].append((n, v)) or {'ok': True, 'value': v})
    monkeypatch.setattr(app_module, '_heatpump_power', lambda: None)
    return env


def _set(key, value):
    db.session.merge(EditableText(key=HP + key, content=value))
    db.session.commit()


def _pump(mode, **data):
    app_module._heatpump_live.update(online=True, slow={'Betriebsart': mode}, data=data, slow_ts=time.time())


# ---------------- A. gate ----------------
def test_gate_pump_off_switches_convectors_off(hp):
    _season('heat')
    _thermo(convs=('К1', 'К2'))
    hp['temp'] = 18.0                                    # demand: on
    hp['status']['К1'] = {'online': True, 'is_on': True, 'power_w': None}
    _pump(0)
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [('К1', False)]                 # К2 (off) is not switched on
    assert app_module._hvac_pump_gate() == {'active': False, 'reason': 'термопомпата е изключена'}


def test_gate_supply_off_and_restart(hp, monkeypatch):
    _season('heat')
    _thermo()
    hp['temp'] = 18.0
    _set('meter', '10.0.0.9')
    monkeypatch.setattr(app_module, '_heatpump_relay', lambda set_on=None: {'available': True, 'is_on': False, 'error': None})
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [] and app_module._hvac_pump_gate()['reason'] == 'централното захранване е изключено'
    monkeypatch.setattr(app_module, '_heatpump_relay', lambda set_on=None: {'available': True, 'is_on': True, 'error': None})
    app_module._heatpump_restart['running'] = True
    hp['status']['К1'] = {'online': True, 'is_on': True, 'power_w': None}
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [('К1', False)]
    d = _login('worker').get('/admin/hvac/data').get_json()
    assert d['pump_gate'] == {'active': False, 'reason': 'рестарт на термопомпата'}
    assert 'id="bnGate"' in _login('worker').get('/admin/hvac').get_data(as_text=True)


def test_gate_unknown_is_active(hp, monkeypatch):
    _season('heat')
    _thermo()
    hp['temp'] = 18.0
    _set('meter', '10.0.0.9')
    monkeypatch.setattr(app_module, '_heatpump_relay', lambda set_on=None: {'available': True, 'is_on': None, 'error': 'Електромерът не отговаря.'})
    app_module._thermostat_tick(NOW)                     # relay unknown, pump offline: normal
    assert hp['sets'] == [('К1', True)]


def test_gate_residual_heat_min_water(hp):
    """Owner's request 2026-11-25: pump off, but heating goes on while the supply water is at least the set minimum."""
    _season('heat')
    _thermo()
    hp['temp'] = 18.0                                    # demand: on
    _pump(0, **{'Temp. Vorlauf': 32.0})
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == []                              # no minimum set: inactive as before
    r = _login('admin').post('/admin/heatpump/residual', data={'residual_min': '30'})
    assert r.status_code == 302 and app_module._heatpump_residual_min() == 30.0
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [('К1', True)]                  # 32 >= 30: works
    assert app_module._hvac_pump_gate()['active'] is True
    app_module._heatpump_live['data'] = {'Temp. Vorlauf': 29.5}
    assert app_module._hvac_pump_gate()['reason'] == 'термопомпата е изключена'
    _set('last_mode', '0')
    app_module._heatpump_live['online'] = False          # no reading (last mode 0): inactive
    app_module._heatpump_live['data'] = {'Temp. Vorlauf': 40.0}
    assert app_module._hvac_pump_gate()['active'] is False
    _pump(0, **{'Temp. Vorlauf': 40.0})
    _season('cool')                                      # cooling: not used
    assert app_module._hvac_pump_gate()['active'] is False
    assert _login('admin').post('/admin/heatpump/residual', data={'residual_min': '99'}).status_code == 302
    assert app_module._heatpump_residual_min() == 30.0   # out of range refused
    _login('admin').post('/admin/heatpump/residual', data={'residual_min': ''})
    assert app_module._heatpump_residual_min() is None


def test_hkp_read_each_param_alone(monkeypatch):
    """HKP settings: registered in HtParams, one failing number does not stop the others, the pump's NAME is kept."""
    from htheatpump import HtParams
    assert all(n in HtParams for n in app_module.HEATPUMP_HKP_PARAMS)
    assert HtParams['HKP_Nachlaufzeit (Sec)'].dp_number == 36
    monkeypatch.setattr(app_module.time, 'sleep', lambda s: None)

    class Ser:
        dropped = 0

        def reset_input_buffer(self):
            Ser.dropped += 1

    class FakeHp:
        _ser = Ser()

        def _get_param(self, name):
            if name == 'HKP_Modus':
                raise IOError('no answer')
            return ('Other' if name == 'HKP n_min(%)' else name), 0, 1, 7

    out = app_module._heatpump_hkp(FakeHp())
    assert out['HKP_Modus'] == {'error': 'no answer'} and Ser.dropped == 1
    assert out['HKP_Nachlaufzeit (Sec)'] == {'val': 7, 'pump_name': 'HKP_Nachlaufzeit (Sec)'}
    assert out['HKP n_min(%)']['pump_name'] == 'Other' and len(out) == len(app_module.HEATPUMP_HKP_PARAMS)


def test_gate_after_app_restart_uses_last_saved_mode(hp):
    """Owner's report 2026-11-25: right after an app restart (pump not read yet) the convectors went on for a minute
    although the pump was off. Before the first read the gate takes the last mode the poller saved."""
    _season('heat')
    _thermo()
    hp['temp'] = 18.0                                    # demand: on
    _set('last_mode', '0')                               # the pump was off before the restart; live is still offline
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [] and app_module._hvac_pump_gate()['reason'] == 'термопомпата е изключена'
    _set('last_mode', '1')
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [('К1', True)]
    hp['sets'].clear()
    _pump(0)                                             # a live read wins over the saved mode
    hp['status']['К1'] = {'online': True, 'is_on': True, 'power_w': None}
    app_module._thermostat_tick(NOW)
    assert hp['sets'] == [('К1', False)]


# ---------------- B. hard restart ----------------
def _fake_restart_env(hp, monkeypatch, relay):
    calls, sleeps = [], []
    live = app_module._heatpump_live

    def fake_relay(set_on=None):
        calls.append(set_on)
        return relay(set_on)

    def fake_sleep(s):
        sleeps.append(s)
        if True in calls and s == app_module.HEATPUMP_RESTART_POLL_S:
            live.update(online=True, slow={'Betriebsart': 0}, slow_ts=time.time() + 1)    # the pump is back, mode lost
        elif s == app_module.HEATPUMP_RESTART_OFF_S:
            live.update(online=False)
    monkeypatch.setattr(app_module, '_heatpump_relay', fake_relay)
    monkeypatch.setattr(app_module, '_heatpump_sleep', fake_sleep)
    _pump(1)
    app_module._heatpump_restart['running'] = True
    return calls, sleeps


def test_hard_restart_sequence(hp, monkeypatch):
    calls, sleeps = _fake_restart_env(hp, monkeypatch, lambda on: {'available': True, 'is_on': on, 'error': None})
    app_module._heatpump_hard_restart('ръчно', 'hvac_admin', 'admin')
    st = app_module._heatpump_restart
    assert calls == [False, True]
    assert sleeps[0] == 30 and sleeps[1:] == [5]
    assert hp['writes'] == [('Betriebsart', 1)]
    assert st['ok'] is True and st['running'] is False and st['mode'] == 1
    assert ActivityLog.query.filter(ActivityLog.action.like('%твърд рестарт%завършен%')).count() == 1


def test_hard_restart_off_error_aborts(hp, monkeypatch):
    calls, sleeps = _fake_restart_env(hp, monkeypatch, lambda on: {'available': True, 'is_on': None, 'error': 'Електромерът не отговаря.'})
    app_module._heatpump_hard_restart('ръчно', 'hvac_admin', 'admin')
    st = app_module._heatpump_restart
    assert calls == [False] and sleeps == [] and hp['writes'] == []
    assert st['ok'] is False and 'не се изключи' in st['error'] and st['running'] is False


def test_hard_restart_on_failure_reported(hp, monkeypatch):
    calls, sleeps = _fake_restart_env(hp, monkeypatch, lambda on: {'available': True, 'is_on': False,
                                                                    'error': None if on is False else 'Електромерът не отговаря.'})
    app_module._heatpump_hard_restart('ръчно', 'hvac_admin', 'admin')
    st = app_module._heatpump_restart
    assert calls == [False, True, True, True] and sleeps == [30, 5, 5]
    assert st['ok'] is False and 'ИЗКЛЮЧЕНО' in st['error'] and hp['writes'] == []


def test_hard_restart_route(hp, monkeypatch):
    spawned = []
    monkeypatch.setattr(app_module, '_heatpump_spawn', lambda fn, *a: spawned.append(a))
    monkeypatch.setattr(app_module, '_heatpump_relay', lambda set_on=None: {'available': False, 'is_on': None, 'error': 'Не е избран електромер (Shelly) за помпата.'})
    c = _login('admin')
    assert c.post('/admin/heatpump/restart').status_code == 400
    monkeypatch.setattr(app_module, '_heatpump_relay', lambda set_on=None: {'available': True, 'is_on': True, 'error': None})
    r = c.post('/admin/heatpump/restart')
    assert r.status_code == 200 and r.get_json()['restart']['running'] is True and len(spawned) == 1
    assert c.post('/admin/heatpump/restart').status_code == 409       # second start refused
    assert len(spawned) == 1
    assert _login('worker').post('/admin/heatpump/restart').status_code == 302
    assert _login('admin').get('/admin/heatpump/data').get_json()['restart']['running'] is True


# ---------------- C. faults + automatic restart ----------------
def _fault(idx, code, ts, msg='Грешка'):
    f = HeatPumpFault(idx=idx, code=code, message=msg, ts=ts)
    db.session.add(f)
    db.session.commit()
    return f


def _active_fault(code, ts):
    app_module._heatpump_live.update(online=True, data={'Stoerung': True},
                                     slow={'Betriebsart': 1, 'last_fault': {'code': code, 'text': 'EQ', 'ts': ts.isoformat()}})


def test_auto_restart_once_per_fault_and_limit(hp, monkeypatch):
    started = []
    monkeypatch.setattr(app_module, '_heatpump_restart_start', lambda reason, *a, **kw: started.append(reason) or {'ok': True})
    _set('restart_codes', json.dumps([5]))
    t0 = datetime.datetime(2026, 11, 25, 8, 0)
    _fault(0, 7, t0)
    _active_fault(7, t0)
    app_module._heatpump_auto_restart_check()
    assert started == []                                 # a code that is not chosen
    f = _fault(1, 5, t0 + datetime.timedelta(minutes=1))
    _active_fault(5, f.ts)
    app_module._heatpump_auto_restart_check()
    app_module._heatpump_auto_restart_check()            # same entry: once
    assert len(started) == 1 and 'грешка 5' in started[0]
    assert db.session.get(HeatPumpFault, f.id).auto_restart is True
    app_module._heatpump_auto['trigger'] = None          # e.g. after an app restart: the row's flag still blocks it
    app_module._heatpump_auto_restart_check()
    assert len(started) == 1
    for i in range(2, 5):
        g = _fault(i, 5, t0 + datetime.timedelta(minutes=i))
        _active_fault(5, g.ts)
        app_module._heatpump_auto_restart_check()
    assert len(started) == 3 and app_module._heatpump_auto_limited()       # the 4th fault is over the limit
    assert db.session.get(HeatPumpFault, g.id).auto_restart is False
    app_module._heatpump_auto['times'] = [time.time() - 7 * 3600] * 3      # window passed
    app_module._heatpump_auto_restart_check()
    assert len(started) == 4


def test_fault_sync_reads_new_entries_and_dedupes(hp):
    base = datetime.datetime(2026, 11, 20, 12, 0)
    entries = [{'index': i, 'error': 10 + i, 'datetime': base + datetime.timedelta(hours=i), 'message': f'E{i}'} for i in range(3)]

    class FakeHp:
        def __init__(self):
            self.asked = []

        def get_fault_list(self, *idx):
            self.asked.append(idx)
            return [entries[i] for i in idx]
    h = FakeHp()
    slow = {'fault_count': 3, 'last_fault': {'ts': entries[2]['datetime'].isoformat()}}
    got = app_module._heatpump_fault_sync(h, slow, None)
    assert h.asked == [(0, 1, 2)]
    assert app_module._heatpump_fault_sync(h, slow, (3, slow['last_fault']['ts'])) == []
    assert app_module._heatpump_fault_sync(h, slow, (2, 'x')) == [entries[2]] and h.asked[-1] == (2,)
    app_module._heatpump_store_faults(got)
    app_module._heatpump_store_faults(got + [entries[2]])
    db.session.commit()
    assert HeatPumpFault.query.count() == 3


# ---------------- D. schedule ----------------
def test_in_pump_schedule():
    rows = [(0, 360, 1080), (6, 600, 1440)]
    assert hvac.in_pump_schedule(rows, NOW, set())
    assert not hvac.in_pump_schedule(rows, NOW.replace(hour=19), set())
    assert hvac.in_pump_schedule(rows, NOW.replace(hour=23, minute=59) + datetime.timedelta(days=6), set())   # Sunday, end 24:00
    assert not hvac.in_pump_schedule(rows, NOW.replace(hour=7), {NOW.date()})                               # holiday = Sunday


def _pump_schedule(on='1'):
    _set('schedule_on', on)
    db.session.add(HeatPumpInterval(weekday=0, start_min=360, end_min=1080))
    db.session.commit()


def test_schedule_tick_writes_mode(hp):
    _season('heat')
    _pump_schedule()
    _pump(0)
    app_module._thermostat_tick(NOW)
    assert hp['writes'] == [('Betriebsart', 1)]
    _pump(1)
    app_module._thermostat_tick(NOW.replace(hour=20))
    assert hp['writes'][-1] == ('Betriebsart', 0)
    _season('cool')
    _pump(0)
    app_module._thermostat_tick(NOW)
    assert hp['writes'][-1] == ('Betriebsart', 2)


def test_schedule_leaves_special_mode_and_off_when_disabled(hp):
    _season('heat')
    _pump_schedule()
    _pump(7)                                             # Парти, set by hand
    app_module._thermostat_tick(NOW.replace(hour=20))
    _pump(1)
    _set('schedule_on', '0')
    app_module._thermostat_tick(NOW.replace(hour=20))
    assert hp['writes'] == []


def test_schedule_save_route(hp):
    c = _login('admin')
    bad = json.dumps([{'weekday': 0, 'start': '06:00', 'end': '12:00'}, {'weekday': 0, 'start': '11:00', 'end': '14:00'}])
    c.post('/admin/heatpump/schedule', data={'enabled': '1', 'rows_json': bad})
    assert HeatPumpInterval.query.count() == 0
    c.post('/admin/heatpump/schedule', data={'rows_json': json.dumps([{'weekday': 0, 'start': '12:00', 'end': '06:00'}])})
    assert HeatPumpInterval.query.count() == 0
    ok = json.dumps([{'weekday': 0, 'start': '05:00', 'end': '24:00'}, {'weekday': 6, 'start': '08:00', 'end': '12:00'}])
    r = c.post('/admin/heatpump/schedule', data={'enabled': '1', 'rows_json': ok})
    assert r.status_code == 302 and r.headers['Location'].endswith('#schedule')
    assert sorted((i.weekday, i.start_min, i.end_min) for i in HeatPumpInterval.query) == [(0, 300, 1440), (6, 480, 720)]
    assert app_module.get_text(HP + 'schedule_on', None) is None    # the flag is the toggle's, not the form's
    assert _login('worker').post('/admin/heatpump/schedule', data={'rows_json': '[]'}).status_code == 302
    assert HeatPumpInterval.query.count() == 2


def test_schedule_toggle_route_and_manual_mode(hp):
    _season('heat')
    _pump_schedule('0')
    c = _login('admin')
    r = c.post('/admin/heatpump/schedule/enabled', data={'enabled': '1'})
    assert r.get_json() == {'ok': True, 'enabled': True} and app_module._heatpump_schedule_on()
    assert c.post('/admin/heatpump/schedule/enabled', data={'enabled': '0'}).get_json()['enabled'] is False
    assert not app_module._heatpump_schedule_on() and HeatPumpInterval.query.count() == 1     # intervals kept
    assert ActivityLog.query.filter_by(action='Термопомпа: график изключен').count() == 1
    assert _login('worker').post('/admin/heatpump/schedule/enabled', data={'enabled': '1'}).status_code == 302
    assert not app_module._heatpump_schedule_on()
    _pump(1)                                             # off: no write, even outside the intervals (hand-set mode stays)
    app_module._thermostat_tick(NOW.replace(hour=20))
    _pump(0)
    app_module._thermostat_tick(NOW)
    assert hp['writes'] == []
    _set('protect_out_heat', '3')                        # protection still acts with the schedule off
    _pump(0, **{'Temp. Aussen': 1.0})
    app_module._thermostat_tick(NOW.replace(hour=20))
    assert hp['writes'] == [('Betriebsart', 1)]
    html = _login('admin').get('/admin/heatpump').get_data(as_text=True)
    assert html.count('data-sched-toggle') == 2 and 'изкл. (ръчно)' in html and 'защитата от замръзване пак действа' in html
    d = _login('admin').get('/admin/hvac/data').get_json()
    assert d['pump']['schedule_on'] is False
    assert 'data-sched-toggle' in _login('admin').get('/admin/hvac').get_data(as_text=True)
    assert 'data-sched-toggle' not in _login('worker').get('/admin/hvac').get_data(as_text=True)


# ---------------- F. protection ----------------
def test_protection_rules():
    cfg = {'out_heat': 3.0, 'room_heat': 5.0, 'out_cool': 35.0, 'room_cool': 28.0}
    assert hvac.protection('heat', 1.5, [], cfg, False) == 'навън 1.5 °C < 3 °C'
    assert hvac.protection('heat', 3.5, [('Цех', 4.8)], cfg, False) == 'Цех 4.8 °C < 5 °C'
    assert hvac.protection('heat', 3.5, [('Цех', None), ('Склад', 6)], cfg, False) is None       # no reading ignored
    assert hvac.protection('heat', 3.5, [], cfg, True) is not None       # hysteresis: still on until 4 °C
    assert hvac.protection('heat', 4.0, [], cfg, True) is None
    assert hvac.protection('cool', 36, [], cfg, False) == 'навън 36 °C > 35 °C'
    assert hvac.protection('cool', 30, [('Офис', 27.5)], cfg, True) is not None and hvac.protection('cool', 30, [('Офис', 27)], cfg, True) is None
    assert hvac.protection('off', -10, [], cfg, False) is None
    assert hvac.protection('heat', -10, [], {k: None for k in cfg}, False) is None


def test_protection_ignores_schedule_and_raises_target(hp):
    _season('heat')
    _pump_schedule()
    _set('protect_out_heat', '3')
    _set('protect_room_heat', '18')
    _thermo(heat=15.0)
    hp['temp'] = 17.0                                    # schedule target 15: no demand without protection
    _pump(0, **{'Temp. Aussen': 5.0})                    # outside the pump's interval (20:00), outdoor fine
    app_module._thermostat_tick(NOW.replace(hour=20))
    assert app_module._hvac_protect['reason'] == 'Цех 17 °C < 18 °C'
    assert hp['writes'] == [('Betriebsart', 1)]          # schedule ignored: the season mode
    _pump(1, **{'Temp. Aussen': 5.0})                    # next tick: the pump is on, the gate open
    app_module._thermostat_tick(NOW.replace(hour=20))
    assert hp['sets'] == [('К1', True)]                  # target raised to 18
    d = _login('admin').get('/admin/hvac/data').get_json()
    assert d['pump']['protection'] == 'Цех 17 °C < 19 °C' and d['thermostats'][0]['target'] == 18.0


def test_protection_save_route(hp):
    c = _login('admin')
    for bad in ('abc', '60', 'nan'):
        c.post('/admin/heatpump/protection', data={'out_heat': bad})
        assert app_module.get_text(HP + 'protect_out_heat', None) is None
    c.post('/admin/heatpump/protection', data={'out_heat': '3', 'room_heat': '5,5', 'out_cool': '', 'room_cool': '28'})
    assert app_module._hvac_protect_cfg() == {'out_heat': 3.0, 'room_heat': 5.5, 'out_cool': None, 'room_cool': 28.0}


# ---------------- E. page ----------------
def test_page_tabs_admin_only(hp):
    _fault(0, 5, datetime.datetime(2026, 11, 25, 8, 0), 'Hochdruck')
    _set('restart_codes', '[5]')
    html = _login('admin').get('/admin/heatpump').get_data(as_text=True)
    for s in ('data-tab="overview"', 'data-tab="schedule"', 'data-tab="faults"', 'Твърд рестарт', 'Hochdruck',
              'name="codes" value="5" checked', 'Защита от замръзване', 'data-confirm='):
        assert s in html, s
    assert _login('worker').get('/admin/heatpump').status_code == 302
    c = _login('admin')
    c.post('/admin/heatpump/restart-codes', data={'codes': ['7', 'x', '5']})
    assert app_module._heatpump_restart_codes() == {5, 7}
