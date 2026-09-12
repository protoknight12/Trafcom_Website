# App API reference

This is not a REST API for outside consumers — it's the JSON/AJAX surface the app's own
templates call via `fetch()`/XHR, alongside the ~200 plain HTML-rendering page routes in
`app.py` (not documented here; those are pages, not API). Everything below either returns
`jsonify(...)` or is the internal tool-call API the AI chat assistant uses. For the *why*
behind any given feature, see [`CLAUDE.md`](../CLAUDE.md); for the Shelly energy-monitoring
routes specifically, see [`SHELLY_API.md`](SHELLY_API.md) — they're only stub-referenced here.

**Auth & CSRF.** Every route below requires a session cookie (`@login_required` or
`@role_required(...)` — noted per endpoint). All are POST-only except where marked GET, and
every state-changing POST needs a valid CSRF token: the `csrf_token` form field for a plain
form post, or an `X-CSRFToken` header for a `fetch()`/XHR JSON body post — see
`templates/partials/csrf_field.html`. A handful of older routes here answer with a bare
`jsonify({'error': ...}), 403` only on their auth-failure path and otherwise `redirect()` on
success (e.g. `admin_toggle_registration`, `admin_create_user`) — those aren't real AJAX
endpoints and are skipped below; only routes whose success path also returns JSON are listed.

## Panel Generator

Backs `/generator`'s save/reload preset picker and server-side DXF export (see Panel Generator
in `CLAUDE.md`).

- **`GET /api/generator-presets`** — `@login_required`. Current user's own saved presets.
  Response: `{status: 'success', presets: [{id, name, settings: <parsed settings_json>}]}`.
- **`POST /api/generator-presets`** — `@login_required`. Form: `name`, `settings_json` (a JSON
  string, validated by attempting to parse it). Overwrite-by-name: an existing preset with the
  same `name` for this user has its `settings_json` replaced rather than creating a second row.
  Response: `{status: 'success', id}` or `400` with `{status: 'error', message}`.
- **`POST /api/generator-presets/<int:preset_id>/delete`** — `@login_required`. `403` if the
  preset isn't the current user's own. Response: `{status: 'success'}`.
- **`POST /api/generator/dxf`** — `@login_required`. Body is **JSON**, not form data (the one
  exception among these endpoints): `{width, height, holes: [{x, y, size, type, rot?, length?,
  clusterMask?, rhombusGap?}]}`. Not a JSON response — returns the built `.dxf` file itself
  (`send_file`, `application/dxf`, `as_attachment=True`). Builds server-side with `ezdxf`
  instead of the old hand-rolled DXF text (which real CAD software choked on, see `CLAUDE.md`);
  `_generator_hole_polygon()`/`_generator_stadium_loop()`/`_generator_tri_half_pts()` mirror the
  client JS's own shape math by hand — keep both in sync if either changes.

## Quick-create endpoints

Admin-only JSON AJAX endpoints backing modals on the catalog/order-creation pages, so building
a Product/Order doesn't require abandoning the page to go create a missing row first. All
return `{status: 'success', ...}` or `{status: 'error', message}` with a `400` (or `403`/`500`
on the noted paths).

- **`POST /api/quick-create-detail`** — admin-only (`403` otherwise, `@login_required` at the
  decorator level). Form: `name`, `material` (a `MaterialPrice.key`), `service_id?`,
  `erp_number?`, `code_number?`, `file` (optional `.dxf`), `manual_price` (required only when
  `file` is omitted), `pdf_file?` (optional `.pdf` reference doc). On success:
  `{status: 'success', detail: {id, name, price}}`. `500` on an unexpected exception (rolls
  back and reports `str(e)` — the one endpoint here that leaks a raw exception message).
- **`POST /api/quick-create-product`** — admin-only. Form: `name`, `description?`,
  `markup_percent?`, `components_json?` (`[{detail_id, quantity}]` — duplicate `detail_id`s are
  summed, not turned into two `ProductDetail` rows; each `detail_id` is validated to exist and
  `quantity >= 1`). Response: `{status: 'success', product: {id, name, price}}` (`price` is the
  computed sell price via `calculate_product_pricing()`).
- **`POST /api/quick-create-material`** — admin-only. Mirrors `admin_add_material()`'s full
  validation: `display_name`, material `type`, `cost_per_m2`, `cutting_speed_mm_per_min` +
  `pierce_time_sec` (skipped/`None` for `rods`/`profiles` — saw-cut, never laser-cut),
  `sheet_length_mm`/`sheet_width_mm`/`thickness_mm`/`height_mm`, optional
  `price_per_kg_m2`/`price_per_kg_m`/`weight_kg`, `erp_number?`, `code_number?`, `brand?`.
  `pierce_time_sec` (seconds/pierce, shop-floor unit) is converted to `pierce_rate_per_min`
  before storage. Rejects a byte-for-byte duplicate variant (`_material_variant_exists()`) and
  an ERP № conflict. Response: `{status: 'success', material: {key, option_text}}` — `key` is
  assigned `material_{id}` post-insert, `option_text` is `format_material_option()`'s rendered
  label, ready to drop straight into a `<select>`.
- **`POST /api/quick-create-instrument`** — requires `current_user.can_manage_quality` (admin
  or `quality_control` role). Form: `name`, `description?`, `accuracy_value?`, `accuracy_unit?`.
  Response: `{status: 'success', instrument: {id, option_text}}`.
- **`POST /api/quick-create-client`** / **`POST /api/quick-create-deliverer`** — `@login_required`
  only (**not** admin-only — any logged-in user places their own orders and needs to add a
  client/deliverer for themselves). Form: `name`, `email?`, `phone?`, `eik?` (validated by
  `_validate_eik()` — must be exactly 9 digits if provided), `vat_number?`, `address?`, `mol?`,
  plus `client_type` (`'company'`/`'individual'`) for the client variant only. Response:
  `{status: 'success', client: {id, name}}` / `{..., deliverer: {id, name}}`.

## Order / production AJAX

- **`GET /geometry/<int:file_id>`** — `@login_required`. The stored shape data for one of the
  current user's own `DxfFile` uploads (or any file, if admin), for `dxf_viewer.js`'s preview
  modal on `/dashboard`. `403` if not owner/admin. Response:
  `{filename, width, height, shapes}`.
- **`GET /details/files/geometry/<int:file_id>`** — `@role_required('admin')`. Same response
  shape as above but for a `DetailDxfFile` revision (admin-only — a rendered preview still
  exposes the design). Parsed on demand each call (no cached `geometry_json` column on this
  table). `400` if the file isn't a `.dxf` or fails to parse.
- **`POST /api/upload-order-item-pdf`** — `@login_required`. Multipart `file` (`.pdf` only).
  Stages a reference PDF for a not-yet-created `OrderItem` line before the cart is submitted;
  `create_order()` links it once the row has an id. Response:
  `{status: 'success', filename, original_filename}`.
- **`POST /admin/orders/<int:order_id>/assign_machine`** — `@login_required`, admin or `worker`
  role checked in-body (`403` otherwise). Form: `machine_id` (blank/non-digit clears the
  assignment). Response: `{status: 'success', machine_name}` (`null` when cleared).
- **`POST /admin/production`** (the POST branch only — GET renders `production_report.html`) —
  `@login_required`, staff-only (`is_staff`) checked in-body. Form: `target_type`
  (`'item'`/`'component'`), `target_id`, `produced_qty`. Clamps `produced_qty` into
  `[0, quantity_needed]`, bumps the linked `Detail.stock_quantity` by the delta via the same
  `_bump_stock()` used by delivery-note intake, and recomputes the order's rollup. Response:
  `{status: 'success', produced_qty, target_percent, item_percent, order_percent, order_status,
  order_status_label}`.

## Quality control

- **`GET /api/quality-template/<target_type>/<int:target_id>`** — `@role_required(['admin',
  'quality_control'])`. `target_type` is `'detail'`/`'product'`. The saved
  `QualityCheckTemplate` for that catalog row, if any (reused as a starting point when picked
  again on `admin_quality_control.html`) — header fields + measurement rows, no sample
  *values* (a template has none). Response: `{status: 'success', template: null}` when none
  exists, else `{status: 'success', template: {drawing_no, batch_size, sample_size, iso8015,
  measurements: [{parameter_name, measurement_type, nominal_value, tolerance_plus,
  tolerance_minus, unit, drawing_ref, instrument_id}]}}`.
- **`POST /admin/quality/template/save`** — same role gate. Saves/replaces the template for a
  `target_type`+`target_id` from the same header + `param_name`/`measurement_type`/
  `nominal_value`/`tolerance_plus`/`tolerance_minus`/`unit`/`instrument_id`/`drawing_ref`
  parallel-array form fields `admin_create_quality_check()` uses (see
  `_build_quality_measurements_from_form()`'s sibling), minus per-inspection-only fields
  (`order_no`, `supervisor_name`, notes, disposition, sample values) that don't belong on a
  reusable template. Requires at least one non-blank `param_name` row. Response:
  `{status: 'success', message}`.

## Offer images

- **`POST /api/upload-offer-item-image`** — `@role_required('admin')`. Multipart `file`
  (`png`/`jpg`/`jpeg`/`webp`/`gif`). Same stage-then-link pattern as the order-item PDF upload
  above — saved immediately, linked into the `OfferItem` row by `_resolve_staged_offer_image()`
  once the offer is actually submitted; an added-then-abandoned row just leaves an orphaned
  file on disk. Response: `{status: 'success', filename, url}`.

## Facility map / panel schematic drag & wiring AJAX

The interactive factory map, panel one-line schematic editor, and solar/battery/temperature
dashboards share one repeating pattern: a small `POST .../position` (or `/scale`) endpoint per
draggable/resizable element, always `@role_required(['admin', 'worker'])`, always form fields
`pos_x`/`pos_y` (or `scale`) as floats clamped to `[0, 100]` (or `[0.4, 3.0]` for scale), always
responding `{pos_x, pos_y}` (or `{scale}`) echoing the clamped value back, and always `400`
`{error: '...'}` on a non-numeric input. One documented instance covers all of them:

| Route | Element |
|---|---|
| `POST /admin/panel-components/<id>/position` | `PanelComponent` on its panel's schematic |
| `POST /admin/panel-components/<id>/scale` | same, resize |
| `POST /admin/battery-stacks/<id>/position` | `BatteryStack` on its room's map |
| `POST /admin/factory-map/convector/<id>/position` | `Convector` on its room's map |
| `POST /admin/factory-map/machine/<id>/position` | `Machine` on its room's map |
| `POST /admin/factory-map/panel/<id>/position` | `ElectricalPanel` on its room's map |
| `POST /admin/factory-map/panel/<id>/overview-position` | same panel, on the **site-wide** overview canvas (`overview_pos_x/y`, a separate coordinate pair from the room-map one above) |

Beyond that shared pattern:

- **`POST /admin/panels/<int:panel_id>/schematic/wires/create`** — `@role_required(['admin',
  'worker'])`. AJAX (draws the new wire immediately client-side, no page reload). Form:
  `from_component_id`, `to_component_id`, `from_pole`, `to_pole` (ints, default `1`),
  `from_side`/`to_side` (`'in'`/`'out'`/`'tap'`, default `'out'`/`'in'`), `phase_type`
  (defaults `'three_phase'` if invalid). Validates poles ≥ 1, no self-connection, both
  components exist and `from_component` belongs to this panel, ≥3 poles on both ends for a
  three-phase wire, no duplicate terminal pairing (checked globally across panels, not just
  this one — `to_component` may belong to a *different* panel entirely, a real cable leaving
  one cabinet toward another). Response on success: `{id, from_component_id, to_component_id,
  from_pole, from_side, to_pole, to_side, phase_type}`; any validation failure is `400`
  `{error: '...'}`.
- **`POST /admin/panel-wires/<int:wire_id>/delete`** — same role. Deleting one pole-row of a
  `three_phase` wire deletes the whole 3-row bundle at once (a single physical connection).
  Response: `{ok: true}`.
- **`POST /admin/panel-wires/<int:wire_id>/retarget`** — same role. Re-points one end (`end`:
  `'from'`/`'to'`) of an existing wire to a different `component_id`/`side` in place, without
  delete-and-redraw; for a `three_phase` bundle this re-targets all 3 pole-rows together, each
  keeping its own pole number against the new target. Same duplicate/pole/type validation as
  wire creation. Response: `{ok: true}` or `400` `{error: '...'}`.
- **`GET /admin/battery-cabinets/data`** — `@role_required(['admin', 'worker'])`. Live
  SOC/voltage/temperature per `BatteryStack` (`_battery_stack_snapshots()`). Response:
  `{ts, stacks: {<stack_id>: <snapshot>}}`.
- **`GET /admin/temperature-sensors/data`** — same role. Live temperature/humidity/battery per
  `TemperatureSensor` (`_mqtt_temp_snapshot()`). Response: `{ts, sensors: {<sensor_id>: <snapshot>}}`.
- **`GET /admin/convectors/data`** — same role. Live on/off state per `Convector`
  (`_shelly_convector_status()`). Response: `{ts, convectors: {<conv_id>: <status>}}`.
- **`GET /admin/factory-map/room/<int:room_id>/data`** — same role. Live power per
  `Machine`/`ElectricalPanel` in the room plus per-`Convector` on/off plus per-`BatteryStack`
  snapshot, all scoped to that room. Response: `{ts, machines, panels, convectors, stacks}`
  (each a dict keyed by id).
- **`GET /admin/factory-map/overview/data`** — same role. Same shape as above but `panels`
  only, site-wide (no room filter). Response: `{ts, panels}`.
- **`POST /admin/modbus-devices/<int:device_id>/read`** — `@role_required('admin')`.
  Diagnostic register dump: form `address`, `count` (1–64), `input_type`
  (`'input'`/`'holding'`, default holding). Reads raw registers over Modbus TCP
  (`_modbus_read_raw()`) and decodes them every plausible way at once so a real-world reading
  can be eyeballed against the meter's own display. Response: `{address, raw, uint16, int16,
  uint32, int32, ...}` (see `_modbus_decode()` for the full key set), or `{error: '...'}` (`400`
  on bad input, `200` with an `error` key on a device-communication failure).

## Solar roof data

- **`GET /admin/solar-roof/data`** — `@role_required('admin')`, `@limiter.exempt` (the app-wide
  300/hour default limiter was starving this page's own 10s poll — see the comment at its
  decorator). Per-string live power keyed `"<inverter_device_id>-<string_number>"` (1–8; each
  Solis MPPT tracker physically combines 2 parallel strings, so both string numbers of a
  tracker report the same shared reading, not an assumed 50/50 split), plus per-inverter
  whole-array summaries and current sun position/rise/set. Response: `{ts, strings: {<key>:
  {voltage, current, power, mppt}}, inverters: {<device_id>: {name, online, power?, today_kwh?,
  yesterday_kwh?, month_kwh?, total_kwh?}}, sun: {azimuth, altitude, is_day, sunrise, sunset,
  path}}`.

## Power / energy data feeds

`GET /admin/power/data` and `GET /admin/power/history` (both `@role_required('admin')`) are the
Shelly energy-monitoring dashboard's live-poll and on-demand-aggregation feeds — **see
[`SHELLY_API.md`](SHELLY_API.md)** for their full response shape, the Gen1/Gen2 payload split,
and the `?host=` scoping param; not re-derived here to avoid the two docs drifting apart.

## AI chatbot tool API

`POST /api/chat` (`@login_required`, `@limiter.limit('20 per hour')`) is the one real HTTP
endpoint — body `{message, history?}` (`history`: last-10-turns, 1000-chars/message cap enforced
server-side regardless of what the client sends), response `{status: 'ok', reply}` or `{status:
'error', message}` (`503` if `ANTHROPIC_API_KEY` isn't configured, `502` on an Anthropic API
error). Everything below is **not** a route — it's the `CHATBOT_TOOLS` list of
`@anthropic.beta_tool`-decorated Python functions the model itself calls during that request
(`anthropic_client.beta.messages.tool_runner()`, `max_iterations=4`). Every tool is read-only,
returns a short Bulgarian text string (not JSON), and queries the DB directly:

| Tool | Parameters | Purpose |
|---|---|---|
| `search_catalog` | `query` | Fuzzy name/brand search across materials, details, products, with stock |
| `list_services` | — | Public services-page machine/service cards |
| `most_expensive_items` | `limit=5` (max 20) | Priciest materials + details, descending |
| `list_materials` | `material_type=''`, `offset=0` | Paged material listing, optional type filter |
| `get_material_details` | `name` | Full spec for one material (fuzzy-matched) |
| `list_products` | `offset=0` | Paged product listing with sell price + stock |
| `get_product_details` | `name` | One product's BOM + price (fuzzy-matched) |
| `my_orders` | `offset=0` | Current user's own orders |
| `order_status` | `order_number` | One of the current user's own orders, by number (never another customer's) |
| `order_missing_stock` | `order_number` | Stock shortfall check for one of the user's own orders |
| `list_service_prices` | — | Public €/hour or €/meter service prices (`Service.show_price` only) |
| `get_machine_details` | `name` | One machine's specs (fuzzy-matched) |
| `list_material_types` | — | Valid `material_type` values for `list_materials` |
| `my_uploads` | `offset=0` | Current user's own DXF upload history |
| `get_upload_details` | `query` | One of the user's own uploads, by filename (fuzzy-matched) |
| `my_profile` | — | Current user's own username/role/email |
| `get_contact_info` | — | Shop address/phone/email (static) |

Plus one non-Python, server-side tool: `{'type': 'code_execution_20260521', 'name':
'code_execution'}` — runs in Anthropic's own sandbox for pure computation, never touches this
app's DB or filesystem.
