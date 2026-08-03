# Shelly energy monitoring — API reference

Live power monitoring off Shelly Pro 3EM energy meters on the shop LAN. Everything lives in
one section of [`app.py`](../app.py) (search for `SHELLY ENERGY MONITORING`) — no new
dependencies, no DB tables, no migration. The meter itself is the archive.

This document is the API reference for that section: every function, its inputs/outputs, the
two HTTP routes, and the limits/gotchas discovered by actually running this against a real
device. For the "why does this feature exist / what did we learn about the shop's wiring"
narrative, see the **Shelly energy monitoring** section in [`CLAUDE.md`](../CLAUDE.md) instead
— this file is the how, that one is the why.

## Hard constraint: read-only, by hardware, not by policy

**The Shelly Pro 3EM has no relay.** It measures current through a clamp; it cannot switch
anything. Every function below only ever issues a `GET` to the device. Nothing here can turn a
machine on or off, and nothing should call a `Switch.Set`/`*.Set` method without a deliberate,
separate decision about machine safety — remote-starting CNC/laser machinery is a regulated
area (EN 60204-1 / EN ISO 12100), not just a coding task. Controlling contactors would require
the separate **Shelly Pro 3EM Switch Add-on** hardware, which is not installed.

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

Polls one meter and returns a render-ready dict. **Never raises** — an unreachable meter
(Wi-Fi drop, panel powered down) is a normal state on a shop floor, and one dead device must
not blank out the whole dashboard or the whole fleet poll.

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

Flattens a `Shelly.GetStatus` payload into `(channels, total_power_W, total_energy_kWh)`.
Internal helper (leading underscore) — called by `shelly_device_snapshot`, not meant to be
called directly, but documented here because its branching is the single most
regression-prone part of this integration.

Handles **both** Shelly EM device profiles, because a meter can be reprofiled at any time via
`Shelly.SetProfile` and this must not silently start rendering nothing:

- **`triphase`** (current profile on the installed meter): one `em:0` component holding three
  phases (`a_*`, `b_*`, `c_*`) of **one 3-phase feed**. Rendered as `Фаза A` / `Фаза B` /
  `Фаза C`.
- **`monophase`**: up to three independent `em1:0`/`em1:1`/`em1:2` components, each its own
  circuit with its own energy counter (`em1data:N`). Rendered as `Вход 1` / `Вход 2` / `Вход 3`.
  Would apply if the clamps were ever rewired to one machine per clamp.

Branches on **key presence** (`'em:0' in status`), not truthiness — a meter mid-reboot sends
`"em:0": {}` (or `null`), which is still a triphase device and must not fall through to the
monophase branch and render zero channels. All field reads use `.get(...) or 0.0`-style
defaults so a partial payload never raises.

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

Covers, using trimmed real captures from the installed meter:

- `_parse_shelly_devices` — blank/bare/labelled/mixed env values.
- `_shelly_readings` — both profiles, partial payloads, null-valued components.
- `_shelly_time_chunks` — exact fits, short tails, sub-step windows, empty windows, no
  gaps/overlap across a multi-chunk range.
- `_parse_shelly_csv` — valid rows, blank cells (partial row survives), unusable timestamps
  (row dropped), empty/header-only bodies.
- `shelly_fleet_snapshot` — monkeypatches `shelly_rpc` with an artificial sleep and asserts a
  5-device poll finishes in under `5 × sleep` (proves it's concurrent, not sequential) and
  that results preserve input order regardless of completion order.

`shelly_rpc`, `shelly_history`, and `shelly_device_snapshot`'s live-network path are not
covered by this script — they were verified by hand against the real device at
`192.168.18.72` while building this feature (see conversation history / commit messages, not
a regression test).

## Extension points, roughly cheapest first

1. **Relabel a device.** Edit `SHELLY_DEVICES` in `.env`, restart. No code change.
2. **Add a second meter.** Add another `Име=host` pair to `SHELLY_DEVICES`. The dashboard's
   cross-machine total summary card (in `admin_power.html`) activates automatically once 2+
   devices are configured — it's hidden with exactly one, since a "total across machines"
   number would just repeat that one machine's own card.
3. **Chart recent history in the UI.** `shelly_history()` already exists and is tested; needs
   a new route (thin wrapper picking a sensible default window, e.g. "today") plus a chart in
   the template. No DB work — the meter answers on demand.
4. **Alerting** (e.g. "phase C exceeded 3kW"). Shelly's own webhook system can push to a Flask
   endpoint on threshold crossings without polling at all — cheaper than watching history for
   this specific case. Currently unconfigured (`Webhook.List` returns empty on the installed
   meter).
5. **Per-machine monitoring once wiring changes.** If clamps are ever rewired to one machine
   per circuit: reprofile the meter to `monophase` (`Shelly.SetProfile`, resets the energy
   counters — plan around that), add a `Machine.shelly_host` column via a
   `migration/migrate_*.py`, and join it in wherever machines are already queried.
6. **Long-term energy history / cost-per-order attribution.** Needs a real Postgres table plus
   a periodic job copying `shelly_history()` output into it before the 45-day window rolls off
   — the meter alone cannot serve this.
7. **Device auth.** If `Shelly.SetAuth` is ever used to password-protect the meters (currently
   `auth_en: false`, meaning anyone on the LAN can factory-reset or reflash one) — add HTTP
   digest auth inside `shelly_rpc()` and `shelly_history()`'s CSV fetch, or every read here
   starts failing with 401.

## Upstream reference

- Gen2 RPC method list on the device itself: `http://<host>/rpc/Shelly.ListMethods`
- [Shelly Gen2 EM component docs](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM)
- [Shelly Pro 3EM product docs](https://www.shelly.com/blogs/documentation/shelly-pro-3em)
- [Shelly Pro 3EM Switch Add-on](https://kb.shelly.cloud/knowledge-base/shelly-pro-3em-switch-add-on) (the
  hardware that would be needed for any future control feature — not installed)
