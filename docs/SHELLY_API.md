# Shelly energy monitoring — API reference

Live power monitoring off Shelly energy meters on the shop LAN. Everything lives in one section
of [`app.py`](../app.py) (search for `SHELLY ENERGY MONITORING`) — no new dependencies, no DB
tables, no migration. The meter itself is the archive.

**Two device generations are in use, and their APIs share nothing.** The shop turned out to
have both a newer Shelly Pro 3EM (Gen2, `GET /rpc/Shelly.GetStatus`) and an older Shelly 3EM
(Gen1, `GET /status`, no `/rpc/` namespace at all) — discovered when the Gen1 one was added to
`SHELLY_DEVICES` and came back `404` against the Gen2-only endpoint this integration originally
assumed everything spoke. `_shelly_get_status()` hides the split; see Functions below.

This document is the API reference for that section: every function, its inputs/outputs, the
two HTTP routes, and the limits/gotchas discovered by actually running this against a real
device. For the "why does this feature exist / what did we learn about the shop's wiring"
narrative, see the **Shelly energy monitoring** section in [`CLAUDE.md`](../CLAUDE.md) instead
— this file is the how, that one is the why.

## Hard constraint: read-only, by policy — not always by hardware

**The Shelly Pro 3EM has no relay at all.** It measures current through a clamp; it physically
cannot switch anything. The older Shelly 3EM (Gen1) is different: it **does** have one onboard
relay (visible as `relays` in its `/status`) — Gen1 hardware was designed as metering-plus-a-
16A-switch, unlike the Pro 3EM which dropped the relay entirely. That relay is real and callable
in principle, but every function below only ever issues a `GET`. Nothing here calls it, and
nothing should call a `Relay.Set`/`Switch.Set`/`*.Set` method without a deliberate, separate
decision about machine safety first — remote-starting or remote-stopping CNC/laser machinery is
a regulated area (EN 60204-1 / EN ISO 12100), not just a coding task, regardless of whether the
hardware in hand happens to support it. (Controlling the Pro 3EM specifically would additionally
require the separate Shelly Pro 3EM Switch Add-on, which is not installed.)

## Configuration

One environment variable, no DB table:

```
SHELLY_DEVICES=Име=host, Друго=host2
```

- Comma-separated `Име=host` pairs. A bare `host` with no `Име=` is allowed and labels itself
  with its own address.
- Unset or blank disables the feature — `/admin/power` renders a "not configured" notice
  instead of erroring.
- Example from this shop: `SHELLY_DEVICES=Главно табло=192.168.18.72`.

Parsed once at import time into the module-level `SHELLY_DEVICES` list of `(name, host)` tuples
by `_parse_shelly_devices(raw)`.

**Why an env var and not a `Machine` table column:** the installed meter runs the `triphase`
profile, meaning its three CT clamps measure one 3-phase feed as a whole — there's no
per-machine mapping to store yet (confirmed by phase-correlation analysis of the meter's own
history: phases A/B correlate at r=+1.000 and stay within ~8% of each other at full load, i.e.
one balanced 3-phase load, not independent circuits). If a meter is ever rewired/reprofiled so
one channel corresponds to one machine, that's the point to add a `Machine.shelly_host` column
(with a hand-written `migration/migrate_*.py`, per this repo's schema-change convention — see
`CLAUDE.md` → Schema changes).

## Functions

### `shelly_rpc(host, method, params=None, timeout=SHELLY_TIMEOUT)`

The one function that actually talks to a meter. Calls any Gen2 Shelly RPC method:

```
GET http://<host>/rpc/<method>?<params as querystring>
```

Returns the parsed JSON response. **Raises** (`urllib.error.URLError`,
`json.JSONDecodeError`, etc.) on any network, HTTP, or parse failure — it does not catch
anything itself. Callers that must survive an offline meter (`shelly_device_snapshot`) catch
around it explicitly.

A single generic caller instead of one wrapper per RPC method, because every Gen2 method has
the same shape. Params are query-string only (scalars), which covers every read method used
here. The full method list any device exposes is at `http://<host>/rpc/Shelly.ListMethods`.

Methods actually used in this app:

| Method | Params | Returns |
|---|---|---|
| `Shelly.GetStatus` | — | Every component's status at once (em, emdata, temperature, wifi, sys...). One call instead of one per component — this is what the live dashboard uses. |
| `Shelly.GetDeviceInfo` | — | Model, gen, firmware, `profile` (`triphase`/`monophase`), `auth_en`. |
| `EM.GetStatus` | `{'id': 0}` | Live measurements only — smaller payload than `Shelly.GetStatus` if you don't need the rest. |
| `EMData.GetStatus` | `{'id': 0}` | Cumulative kWh counters only. |
| `EMData.GetRecords` | `{'id': 0}` | Which time ranges have stored history and how many records — cheap; call before `shelly_history()` to know if there's anything to fetch. |

```python
shelly_rpc('192.168.18.72', 'Shelly.GetDeviceInfo')
# {'name': None, 'id': 'shellypro3em-3c8a1fd0d84c', 'mac': '3C8A1FD0D84C',
#  'model': 'SPEM-003CEBEU', 'gen': 2, 'fw_id': '...', 'ver': '2.0.0',
#  'app': 'Pro3EM', 'auth_en': False, 'profile': 'triphase', ...}

shelly_rpc('192.168.18.72', 'EM.GetStatus', {'id': 0})
# {'id': 0, 'a_current': 1.56, 'a_voltage': 234.3, 'a_act_power': 354.7, ...,
#  'total_current': 12.01, 'total_act_power': 2785.5, ...}
```

**Auth**: not implemented. The meters currently run with `auth_en: false`. If a device
password is ever set (`Shelly.SetAuth` — recommended, since without it anyone on the LAN can
factory-reset or reflash a meter), every call through this function starts returning HTTP 401
until HTTP digest auth is added here.

**Gen2 only.** A Gen1 device answers this with a `404` (no `/rpc/` namespace exists on Gen1
firmware at all) — that's the whole reason `_shelly_get_status()` below exists, rather than
every caller using `shelly_rpc` directly.

### `_shelly_get_status_gen1(host, timeout)` / `_shelly_get_status(host, timeout=SHELLY_TIMEOUT)`

The Gen1 equivalent of `Shelly.GetStatus` isn't an RPC method at all — it's a plain
`GET http://<host>/status`, and the JSON shape has nothing in common with Gen2's (an `emeters`
list instead of `em:0`/`em1:N` components; see `_shelly_readings` below for the full contrast).
`_shelly_get_status_gen1` is that one plain fetch.

`_shelly_get_status` is the function everything else should call — it hides the generation
split entirely:

```python
_shelly_get_status('192.168.18.72')   # Gen2 meter -> tries Shelly.GetStatus, succeeds
_shelly_get_status('192.168.18.78')   # Gen1 meter -> tries Shelly.GetStatus (404), falls
                                       # back to GET /status, succeeds
```

A host is remembered in the module-level `_shelly_gen_cache` dict (`host -> 'gen1'|'gen2'`,
in-memory only, resets on app restart) after its first successful call, so **steady-state
polling of a known Gen1 meter costs one request, not two** — only the very first poll after
each restart pays for the failed Gen2 attempt before falling back. A non-404 HTTP error (a
500, or a 401 from a password-protected meter) propagates rather than being silently
reinterpreted as "must be Gen1" — guarded directly in `testing/test_shelly_status.py`.

### `shelly_fleet_snapshot(devices)`

Polls every configured meter **concurrently** and returns a list of snapshot dicts (see
`shelly_device_snapshot` below) in the same order as `devices`, regardless of which one
answers first.

```python
shelly_fleet_snapshot(SHELLY_DEVICES)
# [{'name': 'Главно табло', 'host': '192.168.18.72', 'online': True, ...}, ...]
```

Backed by a plain `concurrent.futures.ThreadPoolExecutor` (stdlib, no new dependency) sized to
`len(devices)` workers — these are blocking network reads, not CPU work, so the GIL doesn't
matter here. **This is why it's concurrent and not sequential**: with N meters polled
one-by-one, an unreachable meter's full `SHELLY_TIMEOUT` (3s) blocks every meter behind it in
the list, so a fleet with one dead meter could cost `N × 3s` per dashboard refresh. Polling
concurrently means the wall-clock cost is whichever single meter is slowest to answer (or
time out), not the sum. Guarded by `testing/test_shelly_status.py` with a monkeypatched
`shelly_rpc` that sleeps, asserting the whole poll finishes in less than `N × sleep`.

`shelly_fleet_snapshot([])` returns `[]` without spinning up a thread pool.

This is what `GET /admin/power/data` calls — see Routes below.

### `shelly_device_snapshot(name, host)`

Polls one meter (either generation, via `_shelly_get_status`) and returns a render-ready dict.
**Never raises** — an unreachable meter (Wi-Fi drop, panel powered down) is a normal state on a
shop floor, and one dead device must not blank out the whole dashboard or the whole fleet poll.

For a Gen1 device, `temperature` is always `None` (Gen1's `/status` doesn't expose an internal
temperature sensor the way Gen2's `temperature:0` component does) and `rssi` is read from
`wifi_sta.rssi` instead of Gen2's `wifi.rssi` — both handled internally, the returned dict
shape is identical either way.

```python
shelly_device_snapshot('Главно табло', '192.168.18.72')
# online meter:
{
    'name': 'Главно табло', 'host': '192.168.18.72',
    'online': True, 'error': None,
    'channels': [
        {'label': 'Фаза A', 'voltage': 234.3, 'current': 1.56, 'act_power': 354.7,
         'aprt_power': 366.1, 'pf': 0.97, 'freq': 50.0},
        # ... Фаза B, Фаза C
    ],
    'total_power': 2785.5,       # W, all phases summed
    'total_energy': 200.53,      # kWh, cumulative since install
    'temperature': 47.8,         # °C, device's own internal sensor
    'rssi': -52,                 # dBm, Wi-Fi signal
}

# unreachable meter:
{
    'name': 'Главно табло', 'host': '192.168.18.72',
    'online': False, 'error': '<urlopen error timed out>',
    'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
    'temperature': None, 'rssi': None,
}
```

### `_shelly_readings(status)`

Flattens a full-status payload (from `_shelly_get_status`, either generation) into
`(channels, total_power_W, total_energy_kWh)`. Internal helper (leading underscore) — called
by `shelly_device_snapshot`, not meant to be called directly, but documented here because its
branching is the single most regression-prone part of this integration: **three** payload
shapes, not two, and none of them share field names.

- **Gen2 `triphase`** (the installed Pro 3EM's current profile): one `em:0` component holding
  three phases (`a_*`, `b_*`, `c_*`) of **one 3-phase feed**. Rendered as `Фаза A` / `Фаза B` /
  `Фаза C`. Total energy from `emdata:0.total_act`, already in Wh.
- **Gen2 `monophase`**: up to three independent `em1:0`/`em1:1`/`em1:2` components, each its
  own circuit with its own energy counter (`em1data:N`). Rendered as `Вход 1` / `Вход 2` /
  `Вход 3`. A Gen2 meter can be reprofiled between these two at any time via `Shelly.SetProfile`
  — this function must not silently start rendering nothing if that happens.
- **Gen1** (older Shelly EM/3EM, e.g. the second meter at 192.168.18.78): an `emeters` list,
  one dict per channel (`power`, `pf`, `current`, `voltage`, `total`/`total_returned`), no
  `em:0`/`em1:N` involved at all. A 3-channel Gen1 device is always a 3EM measuring one
  3-phase feed (Gen1 has no separate monophase/triphase profile the way Gen2 does), so 3
  channels render as `Фаза A/B/C`; anything else (a 2-channel Shelly EM) renders as
  `Вход 1`/`Вход 2` — independent circuits. Two things Gen1 doesn't report that Gen2 does:
  - **Apparent power** isn't in the payload at all — derived as `voltage * current` (the
    actual definition of apparent power, S = V·I), rather than left blank.
  - **Per-channel frequency** isn't exposed in `/status` — left as `None`.

  Gen1's own cumulative energy counter (`total`) is in **Watt-minutes**, not Wh — divided by
  `60000` for kWh, vs. Gen2's `/1000` (Wh → kWh). Getting this conversion wrong would silently
  under-report energy by 60×, so it's covered directly in `testing/test_shelly_status.py`
  against a real captured payload rather than trusted by inspection.

Branches on **key presence** (`'em:0' in status`, then `'emeters' in status`), not truthiness —
a meter mid-reboot sends `"em:0": {}` (or `null`), which is still a triphase Gen2 device and
must not fall through to a different branch and render zero/wrong channels. All field reads
use `.get(...) or 0.0`-style defaults so a partial payload never raises.

### `shelly_history(host, start_ts, end_ts, em_id=0)`

Pulls minute-resolution history straight off the meter's own flash storage — there is no local
table to keep in sync; the device *is* the archive.

```python
import time
end = int(time.time())
start = end - 3600  # last hour
rows = shelly_history('192.168.18.72', start, end)
# [{'timestamp': 1785743220, 'a_avg_voltage': 235.16, 'a_max_current': 1.58,
#   'c_max_act_power': 2072.0, 'c_min_act_power': 627.8, ...}, ...]  # ~50 fields/row
```

Each record carries, per phase: total/fundamental active energy, returned energy,
lagging/leading reactive energy, and max/min/avg of voltage, current, active power and
apparent power over that minute, plus neutral current. **The min/max spread within a minute is
what separates a cycling load (chiller, compressor) from a steady one** — this is exactly how
the phase-C standing load was identified as a compressor/chiller rather than a constant draw.

Two hard limits, both learned by timing real calls against the installed meter:

- **Retention is ~45–48 days rolling.** Older data is gone for good. Anything that needs to
  outlive that window has to be copied off the meter into Postgres — there is no such copy job
  today; this function is a live pull only.
- **It's slow.** Measured throughput is ~55–60 records/sec (the device is a small embedded CPU
  serializing from flash), so a 1-hour window takes ~1.5s, 8h ~9s, 24h ~25s, 3 days ~75s — and
  a 14-day window in one request **times out and never completes.** `shelly_history()` handles
  this by chunking the request into `SHELLY_HISTORY_CHUNK` (86400s = 1 day) pieces via
  `_shelly_time_chunks()`, each with its own `SHELLY_HISTORY_TIMEOUT` (60s). Don't call this
  from a request handler on a wide window without a loading state — a multi-day pull is a
  multi-second-to-multi-minute operation.

Uses the CSV endpoint (`GET /emdata/<id>/data.csv?ts=&end_ts=&add_keys=true`) rather than the
`EMData.GetData` RPC method, because `GetData` paginates (returns a `next_record_ts` you have
to loop on) while the CSV route returns an entire window in one response per chunk.

**Not currently wired into any route or UI.** This exists as a ready-to-use building block for
whichever gets requested first: a "chart the last shift" view (call this on demand, no
storage), or a "kWh per order/month" report (needs a Postgres table + a periodic copy job,
since this function alone can't outlive the 45-day window). Building both would be premature —
see the note in `CLAUDE.md`.

**Gen2 only, unlike the rest of this integration.** A Gen1 meter passed here will 404 — Gen1
history lives at a different endpoint (confirmed against the real device: `GET
/emeter/<channel>/em_data.csv`, one CSV per channel/phase rather than one call for the whole
meter) with a different CSV shape (`Date/time UTC, Active energy Wh (A), Returned energy Wh
(A)` — energy per interval already in Wh, not the Watt-minute totals `/status` uses) and,
from a quick check, coarser ~10-minute sample spacing rather than Gen2's per-minute. None of
that has been fully mapped out or implemented, since nothing calls `shelly_history()` at all
today — extending it for Gen1 is a real follow-up task, not a same-day add-on, and would need
its own investigation before writing code against it.

### `_shelly_time_chunks(start_ts, end_ts, step=SHELLY_HISTORY_CHUNK)`

Pure helper: tiles `[start_ts, end_ts)` into `step`-sized `(start, end)` pairs with no gaps and
no overlap. `_shelly_time_chunks(0, 90000)` → `[(0, 86400), (86400, 90000)]`. Exists solely
because `shelly_history()` needs it; unit-tested directly since the tiling logic (exact-fit
windows, short tails, windows under one step, empty windows) is easy to get off-by-one wrong.

### `_parse_shelly_csv(text)`

Pure helper: turns the CSV body from `/emdata/<id>/data.csv` into a list of dicts with numbers
parsed (`timestamp` as `int`, everything else as `float`). A row with a blank or unparseable
cell keeps its other, valid fields rather than being dropped entirely — a record written while
the meter was mid-reboot is still worth what it captured. A row with no usable `timestamp` is
dropped outright, since it's unusable for anything time-series related.

### `_parse_shelly_devices(raw)`

Pure helper parsing the `SHELLY_DEVICES` env var, described under Configuration above.

## HTTP routes (in this app, not on the Shelly device)

Both admin-only (`@role_required('admin')`).

### `GET /admin/power`

Renders `templates/admin_power.html` — static chrome. Passes `devices=SHELLY_DEVICES` so the
template can show a "not configured" message when the list is empty; all live data comes from
polling the JSON feed below client-side.

### `GET /admin/power/data`

```json
{
  "ts": "14:32:07",
  "devices": [
    {
      "name": "Главно табло", "host": "192.168.18.72",
      "online": true, "error": null,
      "channels": [
        {"label": "Фаза A", "voltage": 234.3, "current": 1.56, "act_power": 354.7,
         "aprt_power": 366.1, "pf": 0.97, "freq": 50.0},
        {"label": "Фаза B", "...": "..."},
        {"label": "Фаза C", "...": "..."}
      ],
      "total_power": 2785.5,
      "total_energy": 200.53,
      "temperature": 47.8,
      "rssi": -52
    }
  ]
}
```

Polled every 2s by `admin_power.html` (paused via the Page Visibility API when the tab isn't
active). Server-side polling means meters only ever need to be reachable from the app host,
never from every admin's browser — relevant since the meters are LAN-only and have no auth.

Calls `shelly_fleet_snapshot(SHELLY_DEVICES)` — every configured meter concurrently, one HTTP
round-trip to the app per dashboard refresh regardless of fleet size.

The client aggregates a shop-wide total across all `devices[].total_power` itself (see
`admin_power.html`'s `renderSummary()`) rather than the server pre-computing it, since the
payload already carries everything needed — no reason to compute the same sum twice.

## Module-level constants

| Name | Value | Purpose |
|---|---|---|
| `SHELLY_DEVICES` | parsed from env at import time | `[(name, host), ...]` |
| `SHELLY_TIMEOUT` | `3.0` | Per-request timeout for live reads (`shelly_rpc` default) |
| `SHELLY_HISTORY_CHUNK` | `86400` (1 day) | Window size `shelly_history()` splits requests into |
| `SHELLY_HISTORY_TIMEOUT` | `60.0` | Per-chunk timeout for history reads |

## Testing

`testing/test_shelly_status.py` — plain assert script, no network, no fixtures. Run:

```bash
python -m testing.test_shelly_status
```

Covers, using trimmed real captures from both installed meters (Gen2 Pro 3EM at
192.168.18.72, Gen1 3EM at 192.168.18.78):

- `_parse_shelly_devices` — blank/bare/labelled/mixed env values.
- `_shelly_readings` — all three payload shapes (Gen2 triphase, Gen2 monophase, Gen1), partial
  payloads, null-valued components, the Gen1 Watt-minutes→kWh conversion checked against a real
  captured payload (not just trusted by inspection, since a sign/scale error here silently
  under-reports energy by 60×).
- `_shelly_time_chunks` — exact fits, short tails, sub-step windows, empty windows, no
  gaps/overlap across a multi-chunk range.
- `_parse_shelly_csv` — valid rows, blank cells (partial row survives), unusable timestamps
  (row dropped), empty/header-only bodies.
- `shelly_fleet_snapshot` — monkeypatches `shelly_rpc` with an artificial sleep and asserts a
  5-device poll finishes in under `5 × sleep` (proves it's concurrent, not sequential) and
  that results preserve input order regardless of completion order.
- `_shelly_get_status` — monkeypatches both `shelly_rpc` and `_shelly_get_status_gen1` to prove
  the dispatch/cache/fallback logic without any network: a Gen2 host never touches the Gen1
  path; a Gen1 host falls back on the first 404 and is cached so the second call skips the
  Gen2 attempt entirely; a non-404 HTTP error propagates instead of being mistaken for "must be
  Gen1".

`shelly_rpc`, `_shelly_get_status_gen1`, `shelly_history`, and `shelly_device_snapshot`'s
live-network path are not covered by this script — they were verified by hand against the two
real devices while building this feature (see conversation history / commit messages, not a
regression test).

## Extension points, roughly cheapest first

1. **Relabel a device.** Edit `SHELLY_DEVICES` in `.env`, restart. No code change.
2. **Add another meter — any generation.** Add another `Име=host` pair to `SHELLY_DEVICES`; no
   need to know or declare which generation it is, `_shelly_get_status()` figures that out on
   first contact. The dashboard's cross-machine total summary card (in `admin_power.html`)
   activates automatically once 2+ devices are configured — it's hidden with exactly one,
   since a "total across machines" number would just repeat that one machine's own card.
3. **Chart recent history in the UI, for Gen2 meters.** `shelly_history()` already exists and
   is tested; needs a new route (thin wrapper picking a sensible default window, e.g. "today")
   plus a chart in the template. No DB work — the meter answers on demand. Gen1 meters aren't
   covered by this yet — see `shelly_history()`'s docstring/section above for what's confirmed
   and what isn't about its history endpoint.
4. **Gen1 history support**, if a Gen1 meter's history turns out to matter. Same shape of work
   as `_shelly_get_status`'s Gen1/Gen2 split, but for `shelly_history()` — needs its own
   investigation of the confirmed-but-unmapped `/emeter/<channel>/em_data.csv` endpoint (per
   channel, ~10-minute samples, energy already in Wh) before writing code against it.
5. **Alerting** (e.g. "phase C exceeded 3kW"). Shelly's own webhook system can push to a Flask
   endpoint on threshold crossings without polling at all — cheaper than watching history for
   this specific case. Currently unconfigured on both installed meters (`Webhook.List`/
   equivalent returns empty).
6. **Per-machine monitoring once wiring changes.** If a Gen2 meter's clamps are ever rewired to
   one machine per circuit: reprofile it to `monophase` (`Shelly.SetProfile`, resets the
   energy counters — plan around that), add a `Machine.shelly_host` column via a
   `migration/migrate_*.py`, and join it in wherever machines are already queried.
7. **Long-term energy history / cost-per-order attribution.** Needs a real Postgres table plus
   a periodic job copying history output into it before each meter's own retention window
   rolls off — the meters alone cannot serve this.
8. **Device auth.** If `Shelly.SetAuth` (Gen2) or the equivalent Gen1 mechanism is ever used to
   password-protect a meter (currently `auth_en: false` on both installed meters, meaning
   anyone on the LAN can factory-reset or reflash one) — add HTTP digest auth inside
   `shelly_rpc()`, `_shelly_get_status_gen1()`, and `shelly_history()`'s CSV fetch, or every
   read against that meter starts failing with 401.

## Upstream reference

- Gen2 RPC method list on the device itself: `http://<host>/rpc/Shelly.ListMethods`
- [Shelly Gen2 EM component docs](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM)
- [Shelly Pro 3EM product docs](https://www.shelly.com/blogs/documentation/shelly-pro-3em)
- [Shelly Pro 3EM Switch Add-on](https://kb.shelly.cloud/knowledge-base/shelly-pro-3em-switch-add-on) (the
  hardware that would be needed for any future control feature on the Pro 3EM — not installed)
- [Shelly Gen1 API reference](https://shelly-api-docs.shelly.cloud/gen1/) — the older `/status`-based
  API the second, Gen1 meter speaks; no `/rpc/` namespace exists on this generation at all
