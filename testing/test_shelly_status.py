"""
Guards the pure functions behind the Shelly integration - everything except the
ones that actually open a socket (shelly_rpc / shelly_history / _shelly_get_status_gen1):

    _parse_shelly_devices()   SHELLY_DEVICES env parsing
    _shelly_readings()        flattening a full-status payload, both generations
    _shelly_time_chunks()     splitting a history window into per-request chunks
    _parse_shelly_csv()       parsing the history CSV body
    shelly_fleet_snapshot()   concurrent multi-meter poll (shelly_rpc monkeypatched
                              with a sleep, so this one *does* touch threading,
                              just no real network)
    _shelly_get_status()      Gen1/Gen2 dispatch + caching (shelly_rpc and
                              _shelly_get_status_gen1 both monkeypatched)

No network - the payloads below are trimmed captures from a real Shelly Pro 3EM
(Gen2, at 192.168.18.72) and a real older Shelly 3EM (Gen1, "SHEM-3", at
192.168.18.78) - two different device generations turned out to both be
installed in the shop, discovered when the Gen1 one returned a 404 for the
Gen2-only /rpc/ endpoint this integration originally assumed everything spoke.

Four regressions are specifically being guarded:
  - BOTH Gen2 meter profiles must keep working. The Pro 3EM runs 'triphase'
    today, but if the clamps are ever rewired to one machine per circuit it gets
    reprofiled to 'monophase' and the payload shape changes completely
    (em:0 -> em1:0/1/2, different field names).
  - Gen1 devices must keep working alongside Gen2 ones. Gen1 has no /rpc/
    namespace at all (GET /status instead, an `emeters` list, energy in
    Watt-minutes not Wh) - a completely different shape from either Gen2 profile.
  - History windows must stay chunked. A wide window in a single request never
    completes on the device, so _shelly_time_chunks() has to tile the range
    with no gaps.
  - The fleet poll must stay concurrent. It used to poll meters one-by-one, so
    an unreachable meter's timeout serialized onto every other meter's read
    time; shelly_fleet_snapshot() polls them all at once instead.

Run: python -m testing.test_shelly_status
"""
import time
import urllib.error
import app as app_module
from app import (
    _parse_shelly_devices,
    _shelly_readings,
    _shelly_time_chunks,
    _parse_shelly_csv,
    _shelly_get_status,
    shelly_fleet_snapshot,
)

# ---- SHELLY_DEVICES parsing ----
assert _parse_shelly_devices(None) == []
assert _parse_shelly_devices('') == []
assert _parse_shelly_devices('  ,  ') == []

# labelled, bare, and mixed - a bare host labels itself
assert _parse_shelly_devices('Табло=192.168.18.72') == [('Табло', '192.168.18.72')]
assert _parse_shelly_devices('192.168.18.72') == [('192.168.18.72', '192.168.18.72')]
assert _parse_shelly_devices(' Табло 1 = 10.0.0.1 , 10.0.0.2 ') == [
    ('Табло 1', '10.0.0.1'), ('10.0.0.2', '10.0.0.2'),
]

# ---- triphase profile: one em:0 = three phases of one 3-phase feed ----
TRIPHASE = {
    'em:0': {
        'a_current': 1.561, 'a_voltage': 234.3, 'a_act_power': 354.7,
        'a_aprt_power': 366.1, 'a_pf': 0.97, 'a_freq': 50.0,
        'b_current': 1.626, 'b_voltage': 235.7, 'b_act_power': 372.5,
        'b_aprt_power': 383.8, 'b_pf': 0.97, 'b_freq': 50.0,
        'c_current': 8.824, 'c_voltage': 235.4, 'c_act_power': 2058.3,
        'c_aprt_power': 2078.5, 'c_pf': 0.99, 'c_freq': 50.0,
        'total_act_power': 2785.469,
    },
    'emdata:0': {'total_act': 200534.92},
}

channels, total_power, total_energy = _shelly_readings(TRIPHASE)
assert [c['label'] for c in channels] == ['Фаза A', 'Фаза B', 'Фаза C']
assert channels[2]['act_power'] == 2058.3
assert channels[0]['voltage'] == 234.3
assert total_power == 2785.5              # rounded to 1dp
assert total_energy == 200.53             # Wh -> kWh

# ---- monophase profile: three independent em1:N meters ----
MONOPHASE = {
    'em1:0': {'voltage': 233.9, 'current': 1.55, 'act_power': 355.9,
              'aprt_power': 363.0, 'pf': 0.97, 'freq': 50.0},
    'em1:1': {'voltage': 235.6, 'current': 1.63, 'act_power': 378.9,
              'aprt_power': 384.6, 'pf': 0.98, 'freq': 50.0},
    'em1:2': {'voltage': 235.7, 'current': 2.926, 'act_power': 635.7,
              'aprt_power': 690.1, 'pf': 0.92, 'freq': 50.0},
    'em1data:0': {'total_act_energy': 26422.53},
    'em1data:1': {'total_act_energy': 28691.12},
    'em1data:2': {'total_act_energy': 145452.94},
}

channels, total_power, total_energy = _shelly_readings(MONOPHASE)
assert [c['label'] for c in channels] == ['Вход 1', 'Вход 2', 'Вход 3']
assert channels[1]['act_power'] == 378.9
assert total_power == 1370.5              # summed across the three meters
assert total_energy == 200.57

# a monophase device with only one clamp wired reports one channel, not three
channels, total_power, total_energy = _shelly_readings({'em1:0': MONOPHASE['em1:0']})
assert len(channels) == 1
assert total_power == 355.9
assert total_energy == 0.0                # no em1data:0 in this payload

# ---- Gen1 (Shelly 3EM, "SHEM-3"): an `emeters` list, no em:0/em1:N at all ----
# Trimmed from the real device's GET /status response.
GEN1_3EM = {
    'wifi_sta': {'connected': True, 'ssid': 'trafcom-hale', 'rssi': -66},
    'emeters': [
        {'power': 79.12, 'pf': 0.42, 'current': 0.78, 'voltage': 243.1,
         'is_valid': True, 'total': 7811652.2, 'total_returned': 6087.9},
        {'power': 5.08, 'pf': 0.06, 'current': 0.34, 'voltage': 243.36,
         'is_valid': True, 'total': 7093664.2, 'total_returned': 4241.4},
        {'power': 11.78, 'pf': 0.15, 'current': 0.32, 'voltage': 241.98,
         'is_valid': True, 'total': 5150733.5, 'total_returned': 11790.1},
    ],
    'total_power': 95.98,
}

channels, total_power, total_energy = _shelly_readings(GEN1_3EM)
assert [c['label'] for c in channels] == ['Фаза A', 'Фаза B', 'Фаза C']
assert channels[0]['act_power'] == 79.12
assert channels[0]['freq'] is None                       # not reported per-channel on Gen1
# apparent power isn't in the payload - derived as V*I
assert abs(channels[0]['aprt_power'] - 243.1 * 0.78) < 0.001
assert total_power == 96.0                # device's own total_power, preferred over re-summing
# Watt-minutes -> kWh: (7811652.2 + 7093664.2 + 5150733.5) / 60000
assert abs(total_energy - 334.27) < 0.01

# a 2-channel Gen1 device (plain Shelly EM) is independent circuits, not phases
channels, _, _ = _shelly_readings({'emeters': GEN1_3EM['emeters'][:2]})
assert [c['label'] for c in channels] == ['Вход 1', 'Вход 2']

# ---- partial payloads (meter mid-reboot) must not raise ----
channels, total_power, total_energy = _shelly_readings({'em:0': {}})
assert len(channels) == 3
assert channels[0]['voltage'] is None
assert (total_power, total_energy) == (0.0, 0.0)

assert _shelly_readings({}) == ([], 0.0, 0.0)

# null-valued components (the device sends these, not just missing keys)
assert _shelly_readings({'em:0': {'total_act_power': None}, 'emdata:0': None}) == (
    _shelly_readings({'em:0': {}})[0], 0.0, 0.0,
)

# ---- history windowing ----
# Guards the reason chunking exists at all: a wide window in one request never
# completes on the device, so shelly_history() must split it.
assert _shelly_time_chunks(0, 86400) == [(0, 86400)]                  # exact fit
assert _shelly_time_chunks(0, 90000) == [(0, 86400), (86400, 90000)]  # short tail
assert _shelly_time_chunks(0, 3600) == [(0, 3600)]                    # under one step
assert _shelly_time_chunks(100, 100) == []                            # empty window
assert _shelly_time_chunks(0, 10, step=4) == [(0, 4), (4, 8), (8, 10)]
# chunks must tile the window with no gap and no overlap
chunks = _shelly_time_chunks(1785486960, 1785486960 + 3 * 86400)
assert chunks[0][0] == 1785486960 and chunks[-1][1] == 1785486960 + 3 * 86400
assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))

# ---- history CSV parsing ----
CSV = (
    'timestamp,a_avg_voltage,a_avg_current,a_max_act_power\n'
    '1785486960,237.676,0.054,5.3\n'
    '1785487020,238.001,0.056,6.1\n'
)
parsed = _parse_shelly_csv(CSV)
assert len(parsed) == 2
assert parsed[0]['timestamp'] == 1785486960            # int, not float
assert isinstance(parsed[0]['timestamp'], int)
assert parsed[0]['a_avg_voltage'] == 237.676
assert parsed[1]['a_max_act_power'] == 6.1

# a blank cell drops that field only - the rest of the record survives
partial = _parse_shelly_csv('timestamp,a_avg_voltage,a_avg_current\n1785486960,,0.054\n')
assert len(partial) == 1
assert 'a_avg_voltage' not in partial[0]
assert partial[0]['a_avg_current'] == 0.054

# a record with no usable timestamp is useless - dropped entirely
assert _parse_shelly_csv('timestamp,a_avg_voltage\n,237.0\n') == []
assert _parse_shelly_csv('timestamp,a_avg_voltage\nabc,237.0\n') == []

# header-only and empty bodies must not raise
assert _parse_shelly_csv('timestamp,a_avg_voltage\n') == []
assert _parse_shelly_csv('') == []

# ---- fleet polling is concurrent, not sequential ----
# Fake shelly_rpc: sleeps SLEEP_S then returns a per-host power reading, so a
# poll of N devices takes N * SLEEP_S if run sequentially but ~SLEEP_S if
# run in parallel - that gap is what proves shelly_fleet_snapshot() threads it.
SLEEP_S = 0.2


def _fake_rpc(host, method, params=None, timeout=None):
    time.sleep(SLEEP_S)
    return {'em:0': {'total_act_power': float(host.split('-')[1])}}


real_rpc = app_module.shelly_rpc
app_module.shelly_rpc = _fake_rpc
try:
    devices = [(f'M{i}', f'host-{i}') for i in range(5)]
    t0 = time.time()
    results = shelly_fleet_snapshot(devices)
    elapsed = time.time() - t0
finally:
    app_module.shelly_rpc = real_rpc

assert elapsed < SLEEP_S * len(devices), (
    f'fleet poll took {elapsed:.2f}s for {len(devices)} devices at '
    f'{SLEEP_S}s each - looks sequential, not concurrent'
)
# order matches input order regardless of which thread finished first
assert [r['name'] for r in results] == ['M0', 'M1', 'M2', 'M3', 'M4']
assert [r['total_power'] for r in results] == [0.0, 1.0, 2.0, 3.0, 4.0]

assert shelly_fleet_snapshot([]) == []

# ---- Gen1/Gen2 dispatch: try Gen2 first, fall back to Gen1 on a 404, then
# cache the result so steady-state polling of a known Gen1 host costs one
# request instead of two ----
rpc_calls = []
gen1_calls = []


def _rpc_404(host, method, params=None, timeout=None):
    rpc_calls.append(host)
    raise urllib.error.HTTPError(host, 404, 'Not Found', None, None)


def _rpc_ok(host, method, params=None, timeout=None):
    rpc_calls.append(host)
    return {'em:0': {}}


def _fake_gen1_status(host, timeout):
    gen1_calls.append(host)
    return {'emeters': []}


real_rpc, real_gen1 = app_module.shelly_rpc, app_module._shelly_get_status_gen1
app_module._shelly_gen_cache.clear()
try:
    # a Gen2 host: one shelly_rpc call, no Gen1 fallback needed
    app_module.shelly_rpc = _rpc_ok
    app_module._shelly_get_status_gen1 = _fake_gen1_status
    _shelly_get_status('gen2-host')
    assert rpc_calls == ['gen2-host'] and gen1_calls == []

    # a Gen1 host: first call tries Gen2 (404), falls back to Gen1, and caches it
    app_module.shelly_rpc = _rpc_404
    _shelly_get_status('gen1-host')
    assert rpc_calls == ['gen2-host', 'gen1-host']
    assert gen1_calls == ['gen1-host']
    assert app_module._shelly_gen_cache['gen1-host'] == 'gen1'

    # second call to the same Gen1 host skips the Gen2 attempt entirely
    _shelly_get_status('gen1-host')
    assert rpc_calls == ['gen2-host', 'gen1-host']    # unchanged - no new Gen2 attempt
    assert gen1_calls == ['gen1-host', 'gen1-host']   # Gen1 fetched directly this time

    # a non-404 HTTP error (e.g. 500, or an auth-protected meter's 401) must
    # propagate rather than being silently treated as "must be Gen1"
    def _rpc_500(host, method, params=None, timeout=None):
        raise urllib.error.HTTPError(host, 500, 'Server Error', None, None)
    app_module.shelly_rpc = _rpc_500
    try:
        _shelly_get_status('broken-host')
        assert False, 'expected HTTPError to propagate for a non-404 status'
    except urllib.error.HTTPError as e:
        assert e.code == 500
finally:
    app_module.shelly_rpc = real_rpc
    app_module._shelly_get_status_gen1 = real_gen1
    app_module._shelly_gen_cache.clear()

print("ok")
