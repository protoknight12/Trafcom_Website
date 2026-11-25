"""pytest: the HVAC dashboard's weather block - met.no compact payload -> now / every 3 h / next days, and that the
data route never fetches inside the request (no network in tests).

    python -m pytest testing/test_hvac_weather.py -q
"""
from datetime import datetime, timedelta

import app as appmod
from app import _hvac_weather_summary


def _payload(start, hours, temp_of, sym_of):
    ts = []
    for i in range(hours):
        t = start + timedelta(hours=i)
        ts.append({'time': t.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'data': {'instant': {'details': {'air_temperature': temp_of(i), 'wind_speed': 2.2, 'relative_humidity': 46.0}},
                            'next_1_hours': {'summary': {'symbol_code': sym_of(i)}, 'details': {}}}})
    return {'properties': {'timeseries': ts}}


def test_summary_now_hours_days():
    start = datetime(2026, 11, 25, 17, 0)                     # UTC = 19:00 in Sofia
    raw = _payload(start, 5 * 24, lambda i: float(i % 10), lambda i: 'rain' if i % 24 < 12 else 'clearsky_day')
    s = _hvac_weather_summary(raw, now_utc=start + timedelta(minutes=10))
    assert s['now'] == {'temp': 0.0, 'wind': 2.2, 'wind_dir': None, 'humidity': 46.0, 'symbol': 'rain'}
    assert s['hours'][0]['wind'] == 2.2 and s['days'][0]['wind'] == 2.2
    assert [h['h'] for h in s['hours']] == ['22', '01', '04', '07']          # every 3 h, local time, within 12 h
    assert len(s['days']) == 4 and s['days'][0]['date'] == '2026-11-26'      # tomorrow first, today left out
    d = s['days'][0]
    assert (d['min'], d['max']) == (0.0, 9.0) and d['symbol'] in ('rain', 'clearsky_day')


def test_summary_drops_past_and_bad_payload():
    start = datetime(2026, 11, 25, 10, 0)
    raw = _payload(start, 10, lambda i: float(i), lambda i: 'cloudy')
    s = _hvac_weather_summary(raw, now_utc=start + timedelta(hours=5))
    assert s['now']['temp'] == 4.0                            # the entry an hour back is still "now", older ones dropped
    assert _hvac_weather_summary({'nope': 1}) is None
    assert _hvac_weather_summary(raw, now_utc=start + timedelta(days=2)) is None


def test_weather_now_never_blocks_and_fetches_once(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(appmod.threading, 'Thread', FakeThread)
    monkeypatch.setattr(appmod, '_hvac_weather', {'raw': None, 'next': 0.0, 'busy': False})
    assert appmod._hvac_weather_now() is None and len(started) == 1           # due: one background fetch, nothing yet
    assert appmod._hvac_weather_now() is None and len(started) == 1           # busy: no second fetch
