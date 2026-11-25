# Отопление и охлаждане — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-room thermostats with hysteresis + weekly schedule that switch the existing Shelly convectors, a season switch that sets the Heliotherm pump's `Betriebsart`, and a dashboard `/admin/hvac`, all under a new "Отопление и охлаждане" menu.

**Architecture:** Pure decision logic (schedule → target, hysteresis → on/off/keep, clamps) in a new Flask-free module `hvac.py` (precedent: `fscut.py`, `detection.py`). Models, routes, the poller thread and templates in `app.py` under a new section `# ----------------- ОТОПЛЕНИЕ И ОХЛАЖДАНЕ -----------------` placed right after the `# ----------------- КОНВЕКТОРИ -----------------` routes. Convector switching reuses `_shelly_convector_set()` / `_shelly_convector_status()`; pump writes reuse the `_heatpump_cmds` queue.

**Tech Stack:** Flask, SQLAlchemy, Jinja2, vanilla JS + canvas (no chart lib), pytest.

**Spec:** `docs/superpowers/specs/2026-11-25-heating-cooling-design.md`

## Global Constraints

- UI strings in Bulgarian.
- Schema: new tables via `db.create_all()`; new columns only via a NEW `migration/migrate_add_thermostats.py` (`ADD COLUMN IF NOT EXISTS`, safe to re-run). Never edit a shipped migration.
- Season values stored in `EditableText` key `settings.hvac_season`: `'heat' | 'cool' | 'off'`, default `'off'` when unset.
- Season → `Betriebsart`: heat = 1, cool = 2, off = 0.
- Defaults: hysteresis 0.5 K, min on/off 5 min, `lowered_heat` 16 °C, `lowered_cool` NULL (= off).
- Clamps (everyone): heat 5–30 °C, cool 18–30 °C.
- Sensor stale after 40 min (`HVAC_SENSOR_STALE = 2400` s).
- Override: until the next change of the scheduled temperature, max 24 h; `hold` = until released; a season change clears all overrides.
- Holiday = use Sunday's (weekday 6) schedule.
- Intervals: no midnight crossing (`end_min > start_min`, both 0..1440), no overlaps within one thermostat + season + weekday — refused on save.
- `heatpump_circuit`: `heating` → heat only, `cooling` → cool only, `both`/NULL → both.
- Roles: worker → +/-, hold, back-to-schedule. Admin → everything else (season, schedules, thermostats, assignment, holidays). Pump stays admin-only.
- Automatic commands are NOT written to `ActivityLog`; human actions use `log_action()`.
- Poller: one daemon thread, started from `app.py __main__` and `wsgi.py`, never at import; `# ponytail:` note: one per process.
- The working tree already has unrelated uncommitted changes (`migration/run_once.py`, `testing/test_cut_tech_needs_test.py`, `migration/data_laser700_proposed_tech.py`): **never stage them**; `git add` only the files each task names. Ask the owner before any commit/push.

## Review Focus

1. Sensor never reported / deleted sensor → thermostat must treat as stale and switch its convectors off, not crash the tick. (Task 3 test `test_tick_sensor_missing_turns_off`.)
2. Convector offline or `_shelly_convector_set` raises / `MqttNotSent` → tick continues with other convectors, no ConvectorLog row written. (Task 3 test `test_tick_device_error_is_isolated`.)
3. Override set on Friday 16:00 with no schedule change until Monday → expires after 24 h. (Task 1 test `test_override_capped_24h`.)
4. Season switch while pump offline / pump returns another value → season unchanged, error JSON. (Task 3 test `test_season_refused_when_pump_offline`.)
5. Worker POSTs to admin-only routes (season, thermostat save, schedule) → refused. (Task 5 test `test_worker_cannot_change_season_or_schedule`.)

---

### Task 1: Pure logic `hvac.py`

**Files:**
- Create: `hvac.py`
- Test: `testing/test_hvac_logic.py` (pytest, no app import)

**Interfaces:**
- Produces:
  - `HEAT_RANGE = (5.0, 30.0)`, `COOL_RANGE = (18.0, 30.0)`, `OVERRIDE_MAX_H = 24`
  - `clamp(season: str, temp: float) -> float`
  - `scheduled_target(intervals: list[tuple[int,int,int,float]], lowered: float|None, now: datetime, holidays: set[date]) -> tuple[float|None, datetime|None]` — `intervals` = `(weekday, start_min, end_min, temp)` for ONE season; returns the target now and the local datetime of the next change of the target (scan forward up to 7 days, holiday days use weekday 6; None if it never changes).
  - `effective_target(sched_temp, next_change, override_temp, override_until, hold, now) -> tuple[float|None, str]` — source `'schedule' | 'override' | 'hold'`.
  - `override_until(next_change: datetime|None, now: datetime) -> datetime` = `min(next_change or ∞, now + 24 h)`.
  - `decide(season: str, temp: float, target: float|None, hyst: float, is_on: bool) -> bool|None` — heat: `True` if `temp < target-hyst`, `False` if `temp > target+hyst`, else `None`; cool mirrored; `target is None` or season `'off'` → `False`.
  - `validate_intervals(rows) -> str|None` — Bulgarian error text for midnight crossing / overlap / out of 0..1440, else None.

- [ ] **Step 1: Write failing tests**: `test_decide_heat_deadband` (target 21, hyst 0.5: 20.4→True, 21.6→False, 21.0→None), `test_decide_cool_mirrored` (24, 0.5: 24.6→True, 23.4→False), `test_decide_no_target_off`, `test_schedule_interval_and_lowered` (Mon 07:00–17:00 21 °C, lowered 16: Mon 08:00 → (21, Mon 17:00); Mon 18:00 → (16, Tue 07:00)), `test_holiday_uses_sunday`, `test_override_capped_24h` (no intervals at all → next_change None → until = now+24h), `test_effective_target_hold_and_expired`, `test_clamp`, `test_validate_intervals` (22:00–06:00 refused, overlap refused, adjacent 07–12/12–17 ok).
- [ ] **Step 2:** `python -m pytest testing/test_hvac_logic.py -q` → FAIL (no module).
- [ ] **Step 3: Implement `hvac.py`** (stdlib only, naive local datetimes).
- [ ] **Step 4:** same command → PASS.
- [ ] **Step 5: Commit** `hvac.py testing/test_hvac_logic.py` (after the owner's OK).

### Task 2: Models + migration

**Files:**
- Modify: `app.py` (models next to `Convector`; `ConvectorLog`)
- Create: `migration/migrate_add_thermostats.py`
- Test: `testing/test_thermostats.py` (pytest, creates its own SQLite schema via `db.create_all()`)

**Interfaces:**
- Produces:
  - `Thermostat`: `id, name (150, not null), sensor_id FK temperature_sensor.id nullable (index), room_id FK room.id nullable (index), hysteresis Float default 0.5, min_cycle_min Integer default 5, enabled Boolean default True, lowered_heat Float default 16.0, lowered_cool Float nullable, override_temp Float nullable, override_until DateTime nullable (UTC naive), hold Boolean default False, created_at`; relationships `sensor`, `room`, `intervals` (cascade delete-orphan), backref `Convector.thermostat`.
  - `ThermostatInterval`: `id, thermostat_id FK (index), season String(4) 'heat'|'cool', weekday Integer 0..6 (Mon=0), start_min Integer, end_min Integer, temp Float`.
  - `HeatingHoliday`: `id, day Date unique, name String(150)`.
  - `Convector.thermostat_id` Integer FK thermostat.id nullable, index.
  - `ConvectorLog.source` String(12) nullable (`'thermostat'|'manual'`).
- Deleting a `TemperatureSensor` sets `Thermostat.sensor_id` NULL (do it in `admin_delete_temperature_sensor`'s route before delete); deleting a `Thermostat` sets its convectors' `thermostat_id` NULL.

- [ ] **Step 1: Failing test** `test_models_and_migration_columns`: create schema, a Thermostat with one interval and one Convector assigned; assert defaults (0.5, 5, 16.0, None); assert the migration file's column list contains `convector.thermostat_id` and `convector_log.source`.
- [ ] **Step 2:** `python -m pytest testing/test_thermostats.py -q` → FAIL.
- [ ] **Step 3:** Add models + `migration/migrate_add_thermostats.py` (pattern of `migrate_add_order_request_file.py`: `ALTER TABLE convector ADD COLUMN IF NOT EXISTS thermostat_id INTEGER REFERENCES thermostat(id)`, `CREATE INDEX IF NOT EXISTS`, `ALTER TABLE convector_log ADD COLUMN IF NOT EXISTS source VARCHAR(12)`; it must `db.create_all()` first so `thermostat` exists). The model's `index=True` + this migration cover the index; `migrate_add_indexes.py` stays untouched.
- [ ] **Step 4:** PASS. **Step 5:** commit (with OK).

### Task 3: Engine — tick, season, poller

**Files:**
- Modify: `app.py` (new section), `wsgi.py`, `app.py __main__` block
- Test: `testing/test_thermostats.py`

**Interfaces:**
- Consumes: Task 1 functions, Task 2 models, `_shelly_convector_status(conv) -> {'online','is_on','power_w'}`, `_shelly_convector_set(conv, on)`, `_mqtt_temp_snapshot(sensor)`, `TemperatureReading`.
- Produces:
  - `HVAC_SEASON_KEY = 'settings.hvac_season'`, `HVAC_SEASON_MODES = {'heat': 1, 'cool': 2, 'off': 0}`, `HVAC_SEASON_LABELS = {'heat': 'Отопление', 'cool': 'Охлаждане', 'off': 'Изключено'}`, `HVAC_SENSOR_STALE = 2400`, `THERMOSTAT_POLLER_INTERVAL = 60`.
  - `hvac_season() -> str`.
  - `_sensor_reading(sensor) -> {'temp': float|None, 'humidity', 'age_s': int|None}` — age from MQTT `last_seen` else last `TemperatureReading.ts`; None sensor / no data → temp None, age None.
  - `_thermostat_state(th, now_local) -> dict` with keys `temp, humidity, age_s, stale, target, source ('schedule'|'override'|'hold'), next_change (iso|None), override_until (iso|None), demand (bool|None)` — used by both tick and dashboard.
  - `_convector_on_season(conv, season) -> bool` (circuit rule).
  - `_thermostat_switch(conv, on: bool, source: str)` — `_shelly_convector_set` then add `ConvectorLog(convector_id, ts=now, is_on=on, source=source)`; raises on device error (caller catches).
  - `_thermostat_tick()` — for each enabled thermostat in season heat/cool: state; stale → desired False (ignores min time); else `decide()`; `None` = keep; per assigned convector on the season (`_convector_on_season`): read status (offline → skip; `is_on None` → use last ConvectorLog, resend if that row is older than 600 s); skip if desired == current; skip if last ConvectorLog row of that convector is younger than `min_cycle_min` (except stale-off); else `_thermostat_switch(..., 'thermostat')`. Each convector in its own try/except → `_poller_failed('thermostat-device')`. Commits once at the end.
  - `_heatpump_write(name, value) -> dict` — extracted from `admin_heatpump_set()` body (queue + wait + read-back check); `admin_heatpump_set()` calls it. Returns `{'ok': True, 'value'}` or `{'ok': False, 'error', 'status': int}`.
  - `_hvac_set_season(season) -> dict` — `_heatpump_write('Betriebsart', HVAC_SEASON_MODES[season])`; on ok: store key, clear every thermostat's override/hold, if season == 'off' switch OFF every convector with a thermostat (`source='thermostat'`), `log_action(...)`; on failure leave the key unchanged.
  - Route `POST /admin/hvac/season` (`@limiter.limit('30 per hour')`, `@role_required('admin')`, form `season`) → JSON.
  - `start_thermostat_poller()` + `POLLER_STALE_AFTER['thermostat'] = 3 * THERMOSTAT_POLLER_INTERVAL`; started in `wsgi.py` tuple and `__main__`.
  - In `_convector_log_tick()`: unchanged (the thermostat's own row makes the sampler see no diff).
  - In `admin_convector_toggle()`: the `ConvectorLog` row with `source='manual'` is added right after a successful set.

- [ ] **Step 1: Failing tests** (monkeypatch `app._shelly_convector_status`, `app._shelly_convector_set`, `app._mqtt_temp_snapshot`, `app._heatpump_write`; freeze now via a `now` argument on `_thermostat_tick(now=None)`):
  `test_tick_heats_below_band`, `test_tick_deadband_no_command`, `test_tick_cool_season_mirrored`, `test_tick_min_cycle_blocks`, `test_tick_sensor_missing_turns_off` (sensor_id None and stale 41 min — off even inside min time), `test_tick_circuit_filter` (heating-circuit convector untouched in cool), `test_tick_device_error_is_isolated` (one raises MqttNotSent, the other still switched, no log row for the failed one), `test_tick_disabled_or_season_off_no_commands`, `test_tick_writes_log_with_source`, `test_season_writes_betriebsart_and_clears_overrides`, `test_season_off_switches_convectors_off`, `test_season_refused_when_pump_offline` (fake returns ok False → key unchanged, 5xx JSON).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `python -m pytest testing/test_thermostats.py -q` and `python -m pytest testing/test_heatpump.py -q` → PASS (the extraction must not break the pump tests). **Step 5:** commit (with OK).

### Task 4: Menu + Термостати admin page

**Files:**
- Modify: `templates/partials/navbar.html` (lines ~119–138), `templates/partials/subnav.html` (`SUBNAV_GROUPS` key `hvac`), `app.py`, `templates/admin_convectors.html` (thermostat column + warning on toggle of an assigned convector via `data-confirm`)
- Create: `templates/admin_thermostats.html`
- Test: `testing/test_thermostats.py`

**Interfaces:**
- Produces routes (all `@role_required('admin')` except the GET list `['admin','worker']`):
  - `GET /admin/thermostats` `admin_thermostats()` — table (name, sensor, room, convectors, hysteresis, min, enabled), "+ Нов термостат" dialog (`data-dialog-section`), edit via `data-dialog`/`data-values`; convectors as checkboxes, those in the thermostat's room first, those without a described `heatpump_circuit` marked "веригата не е описана"; holiday list + add/delete form.
  - `POST /admin/thermostats/save` `admin_thermostat_save()` (id optional) — validates hysteresis 0.1–3, min 0–60, lowered clamps; sets the convectors' `thermostat_id` (unchecked ones of this thermostat → NULL).
  - `POST /admin/thermostats/<id>/delete`, `POST /admin/thermostats/<id>/schedule` (form `season`, `rows_json` = `[{weekday,start:"HH:MM",end:"HH:MM",temp}]`, replaces that season's rows; `validate_intervals` + `clamp`), `POST /admin/thermostats/<id>/schedule/copy` (form `season`, `target_ids`), `POST /admin/hvac/holidays`, `POST /admin/hvac/holidays/<id>/delete`.
  - Schedule editor: a dialog per thermostat with a 7-row week grid (one row per day, intervals as chips, "+ интервал"), buttons "Работни дни 07–17" and "Почивни = понижена" that fill the grid, "Копирай към…" multi-select. Pure JS in the template, posts `rows_json`.
  - Navbar: inside "Съоръжения и енергия" replace the three links Температурни сензори / Конвектори / Термопомпа with a nested `dropdown-sub` "Отопление и охлаждане" containing Дашборд (`admin_hvac`), Термостати, Температурни сензори, Конвектори, and Термопомпа (admin only).
- [ ] **Step 1: Failing tests**: `test_thermostat_crud_and_assignment`, `test_schedule_save_validates` (overlap → flash error, nothing saved; ok → rows replaced), `test_schedule_copy`, `test_holiday_add_delete`, `test_navbar_has_hvac_section` (GET `/admin/thermostats` as admin contains "Отопление и охлаждане" and `href="/admin/hvac"`).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS. **Step 5:** commit (with OK).

### Task 5: Dashboard

**Files:**
- Modify: `app.py`
- Create: `templates/admin_hvac.html`
- Test: `testing/test_thermostats.py`

**Interfaces:**
- Consumes: `_thermostat_state`, `hvac_season`, `_heatpump_live`, `_heatpump_power()`, `HEATPUMP_MODES`, `_shelly_convector_status` (parallel via `ThreadPoolExecutor` like `_convector_log_tick`).
- Produces:
  - `GET /admin/hvac` `admin_hvac()` (`['admin','worker']`), `{% set subnav = 'hvac' %}`.
  - `GET /admin/hvac/data` (`['admin','worker']`, `@limiter.exempt`) → `{ts, season, season_label, pump: {online, fault, mode, mode_label, mismatch, supply, outdoor, power_w} (pump block only for admins, else null), thermostats: [{id, name, group, state…(_thermostat_state keys), enabled, convectors: [{id, name, online, is_on, power_w, on_season}]}], convectors_on}`. `mismatch` = pump online and `Betriebsart` ≠ `HVAC_SEASON_MODES[season]`.
  - `GET /admin/hvac/<id>/history?hours=24` → `{temps: [[ts,t]], target: [[ts,t]] (sampled from current schedule), on: [[ts_from, ts_to]]}` from `TemperatureReading` + `ConvectorLog` (union of the thermostat's convectors).
  - `POST /admin/hvac/<id>/override` (`['admin','worker']`, `@limiter.limit('120 per hour')`): form `action` = `plus|minus|set|hold|release`, `temp` for set; step 0.5 °C from the current effective target; clamp; `override_until` from `hvac.override_until()`; `hold` stores `hold=True`; `release` clears; `log_action`. JSON = new `_thermostat_state`.
  - Page: top bar (season segmented switch for admins with `data-confirm`, pump card, "N конвектора работят"), yellow banner on `mismatch`, red banner on pump fault; cards grouped by building/room (thermostat room → sensor room → "Без помещение"), each: big temp + humidity, target with −/+ and the source line ("по график до 17:00" / "ръчно до 17:00" / "задържано"), age of the reading, convector chips (on/off/offline, W), 24 h canvas sparkline (temp line, target dashed, shaded on-periods); colour by deviation (heat: blue below / red above band); grey when disabled or season off; red border when stale or a convector offline. Uses `startPoll(fn, 15000)`, `textContent` only (no `innerHTML` with device/DB text), one column under 700 px.
- [ ] **Step 1: Failing tests**: `test_dashboard_loads_for_worker_and_admin`, `test_data_hides_pump_from_worker`, `test_data_mismatch_flag`, `test_override_plus_minus_clamped_and_capped`, `test_worker_cannot_change_season_or_schedule` (POST season / save / schedule as worker → refused, nothing changed), `test_history_shape`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS + `python -m pytest testing/test_smoke_all_pages.py -q`. **Step 5:** commit (with OK).

### Task 6: Docs + live check

**Files:**
- Modify: `CLAUDE.md` (new subsection "Отопление и охлаждане" after "Heliotherm heat pump"; the migration in the list; the two test files in the Tests list)

- [ ] **Step 1:** Write the CLAUDE.md section (models, season key/mapping, tick rules, roles, routes, `hvac.py`, migration, tests).
- [ ] **Step 2:** `python -m testing.run_all thermostat hvac heatpump convector smoke` → all pass.
- [ ] **Step 3:** Preview on a throwaway SQLite DB seeded with 2 thermostats / 3 convectors (fakes for devices), open `/admin/hvac` in the built-in browser at desktop and mobile width, screenshot.
- [ ] **Step 4:** Ask the owner to commit/push; deploy = `deploy/update.sh` (new columns + migration).
