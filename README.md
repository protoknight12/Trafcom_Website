# Trafcom DXF Calculate

![Python](https://img.shields.io/badge/python-3-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/flask-3776AB?logo=flask&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/postgresql-4169E1?logo=postgresql&logoColor=white)
![UI language](https://img.shields.io/badge/UI-Bulgarian-4B8BBE)

A Flask app for a CNC/laser cutting company (Trafcom): upload a DXF drawing and
get its cutting price calculated automatically from geometry (area, cut
length, pierce count), build a catalog of reusable "Details" and "Products"
from priced drawings, track stock via delivery notes, and run customer
orders through production tracking. The UI is in Bulgarian.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running it](#running-it)
- [Deployment](#deployment)
- [Project structure](#project-structure)
- [Schema changes](#schema-changes)
- [Tests](#tests)
- [Configuration notes](#configuration-notes)

## Features

- **DXF pricing** — upload a DXF file, get area/cut-length/pierce-count
  extracted from the geometry and run through a time-based pricing engine:
  material supplies the raw-stock area cost plus its cutting/pierce speed,
  and one or more billable `Service`s (see below) supply the EUR/hour
  rate — a job can be priced against several services at once (e.g. a
  combined cut+engrave pass), plus a flat setup fee.
- **Services & Operations** — admins define billable `Service` types (e.g.
  laser cutting, 3-axis milling) with an EUR/hour rate and the machines
  that can run them; extra post-processing steps on a catalog `Detail`
  (`Operation` — milling, deburring, welding, ...) are costed by
  duration × the linked service's rate, or by length for services priced
  per metre.
- **Catalog** — admin-curated `Detail` (single part) and `Product`
  (assembled from multiple Details + extra costs like paint/assembly, with
  markup) records, each with stock tracking and a printable barcode label.
- **Orders & production tracking** — customer orders (`Order` /
  `OrderItem`) roll up completion status per-component as production
  reports units produced, driven off frozen recipe snapshots so later
  catalog edits don't retroactively change placed orders.
- **Production wizard** — separately from order tracking above, plan a
  standalone "produce N of this Detail" job ahead of demand; it's clamped
  so it can never drive the chosen material batch's stock negative, and
  completing/deleting one properly moves (or reverses) real stock.
- **Delivery notes / stock intake** — recording goods received from a
  `Supplier` bumps stock on the referenced material/detail/product.
  **Material requests** raise the restock *ask* on the other end, from the
  storage dashboard, when stock runs low.
- **Clients & deliverers** — lightweight lookup catalogs (with Bulgarian
  company legal fields — ЕИК, ДДС №, address, МОЛ) an order can reference.
- **Label printing** — Code128 barcode labels (rendered as inline SVG, no
  external service) for any catalog entry or produced batch.
- **Role-based access** — `regular_user`, `worker`, `admin`, `web_designer`,
  with a scoped-down content editor for non-pricing public-page text; new
  registrations can be locked from `/admin/users` without affecting
  existing accounts.
- **Two-factor auth & email verification** — optional per-user TOTP 2FA
  (QR-code setup, no image file written to disk) and email verification/
  password-reset links, both signed & time-limited rather than stored as a
  token column.
- **Quotes (Offers)** — pre-order quotes with product/detail/text-only
  lines, an optional whole-quote discount and expiry date, per-line photos,
  and one click to turn the checked lines into a real `Order`.
- **Offer / protocol / certificate documents** — browser-print pages
  (Ctrl+P to PDF), no server-side PDF library.
- **Panel Generator** — a parametric panel/hole-pattern DXF tool (square,
  hexagon, triangle, rhombus, hex-cluster), with save/reload presets and a
  server-side (`ezdxf`) export that real CAD software can open cleanly.
- **AI chat assistant** *(optional)* — a chat widget backed by Claude, with
  read-only tools to look up catalog items, stock, orders, and prices in
  plain Bulgarian; degrades to a friendly "unavailable" message if no API
  key is configured.
- **Activity log** — every state-changing action by a logged-in user is
  recorded (who, what, when), exportable to `.xlsx`.
- **Missing-stock dashboard** — every open order that doesn't currently have
  enough Detail/Product stock to fulfill it, recomputed live rather than
  stored.
- **DXF revision history per Detail** — the original uploaded `.dxf` files
  behind a catalog Detail are kept and downloadable (admins only), not just
  the geometry extracted from them.
- **Order-time customization** — a standalone-detail order line can carry
  its own ad-hoc operations (independent of any `Operation` permanently
  attached to the Detail catalog row) and an optional reference PDF
  (drawing/spec), both picked at order-creation time and frozen into that
  line's price and paperwork.
- **Live power monitoring** — a real-time dashboard (`/admin/power`)
  polling Shelly energy meters on the shop LAN (both Gen1 and Gen2 devices
  supported), plus a background poller that logs readings locally every
  minute so historical charts are available for both generations — not
  just the roughly 45-day on-device retention Gen2 meters offer natively;
  see
  [`docs/SHELLY_API.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/SHELLY_API.md).
- **Press-brake programming (in progress)** — offline bend programming and
  simulation for the shop's press brakes: unfolding sheet-metal parts into
  flat patterns, choosing tools and bend sequences, and linking programs to
  details and orders. See
  [`docs/BENDING_MODULE.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/BENDING_MODULE.md).
- **Sheet-metal flashing calculator** — flat patterns for L, U, Z and hat
  profiles (with optional 180° hems), calculated live and downloadable as a
  DXF ready for laser cutting.
- **3D hall and plant map** — an interactive 3D model and floor plan of the
  hall showing machines, panels, cable runs and the solar roof, each linked to
  its machine record.
- **Heat pump monitoring** — live readings and history from the heat pump,
  plus a running-cost split into heating, cooling and standby.
- **Solar inverter and battery monitoring** — Modbus-connected inverters and
  battery stacks, polled and logged alongside the Shelly meters.
- **Camera live view and object detection** — live camera streams, with
  object detection running on the CPU and its results logged.
- **LAN network inventory** — the shop network's devices, DHCP hosts and
  links on a map, MAC-address tracing and a checklist-based security audit.
- **ISO 9001 quality records** — controlled documents, internal audits,
  corrective actions (CAPA), risk register, instrument calibration, training
  records, management reviews and customer satisfaction.

## Requirements

- Python 3 (CI and the production server use 3.12)
- PostgreSQL (reachable via `DATABASE_URL`)

## Installation

```bash
pip install -r requirements.txt
```

Running the tests also needs the dev requirements: `pip install -r requirements-dev.txt` (adds `pytest`).

Copy `.env.example` to `.env` and fill in real values:

```
SECRET_KEY=
DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/cnc_calculator_db
FLASK_DEBUG=0
```

`SECRET_KEY` and `DATABASE_URL` are required — the app raises an error on
import if either is missing. `.env` is loaded automatically via
`python-dotenv` and is gitignored.

`.env.example` is the complete, commented list of environment variables
(MQTT, camera detection tuning, reading retention and the rest are all there,
so this README does not repeat them). Everything besides the two required
values is optional, and each feature just degrades gracefully without it:
`ANTHROPIC_API_KEY` (+ optional
`ANTHROPIC_WORKSPACE_ID`) powers the AI chat assistant; `SMTP_HOST` /
`PORT` / `USER` / `PASSWORD` / `FROM` control outgoing password-reset and
email-verification mail (defaults to a local `localhost:25` transport with
no auth if unset).

## Running it

```bash
python app.py
```

This also creates any missing database tables (`db.create_all()` — it does
**not** migrate existing ones, see [Schema changes](#schema-changes) below)
and seeds default material types, prices, services and machine cards on first run, then
opens `http://127.0.0.1:5000/` in your browser. Set `FLASK_DEBUG=1` for the
interactive debugger and auto-reload during local dev — never in a public
deployment.

There is no auto-created admin account. To create or reset one:

```bash
python -m migration.change_admin_password
```

For production, serve `wsgi.py` with gunicorn/waitress instead of running
`app.py` directly (the WSGI entrypoint never runs the dev-server
initialization block above, so run `python app.py` once by hand first to
set up the database).

## Deployment

Production runs on Linux: gunicorn serves `wsgi:app` as a single worker
process with 8 threads, on localhost port 8000, under the systemd unit in
[`deploy/trafcom.service`](https://github.com/protoknight12/Trafcom_Website/blob/main/deploy/trafcom.service).
nginx sits in front of it, serves `/static/` straight from disk and proxies
everything else, including the camera stream. `gunicorn.conf.py` raises the
worker timeout to 120 s.

- [`deploy/update.sh`](https://github.com/protoknight12/Trafcom_Website/blob/main/deploy/update.sh)
  — for releases that change the database or dependencies. It pulls the
  code, dumps the database with `pg_dump` into `backups/` before anything
  touches the schema (last 10 kept; `SKIP_BACKUP=1` skips the dump), installs
  the requirements with the versions pinned in
  [`deploy/constraints.txt`](https://github.com/protoknight12/Trafcom_Website/blob/main/deploy/constraints.txt),
  stops the service, runs every new migration once via
  `python -m migration.run_once`, starts the service again and waits for
  `GET /health`. If a step fails, the service is started again.
- [`deploy/update_quick.sh`](https://github.com/protoknight12/Trafcom_Website/blob/main/deploy/update_quick.sh)
  — for code, template and static-only releases: pull, restart, wait for
  `GET /health`.

`GET /health` answers 200 while the database responds; it also reports the
age of each background poller's last success.

[`deploy/nginx_trafcombg.conf`](https://github.com/protoknight12/Trafcom_Website/blob/main/deploy/nginx_trafcombg.conf)
is a reference copy of the nginx config. The scripts never touch nginx, so
the live file on the server is edited by hand; keep this copy in step with it.

## Project structure

<details>
<summary>Click to expand</summary>

- [`app.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/app.py) — models, DXF geometry/pricing logic, and all routes (single file, by design; the routes are not split into blueprints)
- [`bending/`](https://github.com/protoknight12/Trafcom_Website/tree/main/bending) — press-brake geometry package (unfolding, tools, sequencing, DXF), kept free of Flask so it can be used on its own
- [`detection.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/detection.py) — camera object-detection, used by the detection worker in `app.py`
- [`wsgi.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/wsgi.py) — production entrypoint (gunicorn/waitress)
- [`gunicorn.conf.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/gunicorn.conf.py) — bumps the worker timeout to 120s (`/admin/power/history` can take ~25s per day-chunk fetched, close to gunicorn's 30s default)
- [`requirements.txt`](https://github.com/protoknight12/Trafcom_Website/blob/main/requirements.txt)
- [`.env.example`](https://github.com/protoknight12/Trafcom_Website/blob/main/.env.example)
- [`deploy/`](https://github.com/protoknight12/Trafcom_Website/tree/main/deploy) — systemd unit, nginx reference config and the update scripts (see [Deployment](#deployment))
- [`.github/workflows/tests.yml`](https://github.com/protoknight12/Trafcom_Website/blob/main/.github/workflows/tests.yml) — CI test run
- `uploads/` — private scratch folder for in-flight DXF uploads (not on GitHub — gitignored)

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/templates"><code>templates/</code></a> — Jinja2 templates, one per page (the list below is a selection)</summary>

  - [`about.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/about.html)
  - [`account.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/account.html)
  - [`account_2fa_setup.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/account_2fa_setup.html) — TOTP 2FA setup, QR code rendered as inline SVG
  - [`admin.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin.html)
  - [`admin_client_delivery_note_print.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_client_delivery_note_print.html)
  - [`admin_client_delivery_notes.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_client_delivery_notes.html)
  - [`admin_clients.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_clients.html)
  - [`admin_delivery_note_print.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_delivery_note_print.html)
  - [`admin_delivery_notes.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_delivery_notes.html)
  - [`admin_details.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_details.html)
  - [`admin_generator_presets.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_generator_presets.html) — every user's saved Panel Generator presets
  - [`admin_log.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_log.html) — activity-log audit trail
  - [`admin_material_history.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_material_history.html)
  - [`admin_materials.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_materials.html)
  - [`admin_missing_stock.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_missing_stock.html)
  - [`admin_offer_edit.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_offer_edit.html)
  - [`admin_offer_print.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_offer_print.html)
  - [`admin_offers.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_offers.html)
  - [`admin_power.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_power.html)
  - [`admin_production_orders.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_production_orders.html) — the production wizard (build stock ahead of demand)
  - [`admin_products.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_products.html)
  - [`admin_services.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_services.html) — manage billable `Service` types (EUR/hour rate, linked machines)
  - [`admin_users.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_users.html)
  - [`certificate.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/certificate.html)
  - [`contact.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/contact.html)
  - [`content_editor.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/content_editor.html)
  - [`detail_dxf_dashboard.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/detail_dxf_dashboard.html) — per-Detail DXF revision history
  - [`edit_window.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/edit_window.html)
  - [`error.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/error.html) — shared template for every custom error page (404/403/429/500)
  - [`forgot_password.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/forgot_password.html)
  - [`generator.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/generator.html) — parametric panel/hole-pattern DXF generator
  - [`index.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/index.html)
  - [`label.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/label.html)
  - [`library.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/library.html) — personal DXF upload library (`/dashboard`)
  - [`login.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/login.html)
  - [`login_2fa.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/login_2fa.html) — TOTP code prompt for accounts with 2FA enabled
  - [`machines.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/machines.html)
  - [`my_orders.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/my_orders.html)
  - [`offer.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/offer.html)
  - [`order_create.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/order_create.html)
  - [`product_edit.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/product_edit.html)
  - [`production_report.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/production_report.html)
  - [`protocol.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/protocol.html)
  - [`register.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/register.html)
  - [`reset_password.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/reset_password.html)
  - [`services.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/services.html)
  - [`storage_dashboard.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/storage_dashboard.html)
  - [`storage_details.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/storage_details.html)
  - [`storage_materials.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/storage_materials.html)
  - [`storage_products.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/storage_products.html)
  - [`storage_requests.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/storage_requests.html) — MaterialRequest dashboard (restock asks, separate from DeliveryNote intake)
  - [`upload.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/upload.html)

  <details>
  <summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/templates/partials"><code>partials/</code></a> — shared chrome (navbar, footer, CSRF field, editable-text blocks)</summary>

    - [`navbar.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/navbar.html)
    - [`footer.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/footer.html) — also renders the AI chat widget for logged-in users
    - [`editable.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/editable.html)
    - [`csrf_field.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/csrf_field.html)
    - [`material_options.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/material_options.html)
    - [`legal_fields.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/legal_fields.html) — shared ЕИК/ДДС №/address/МОЛ fields (Client/Deliverer/Supplier)
    - [`favicon.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/favicon.html)
    - [`breadcrumbs.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/breadcrumbs.html)
    - [`local_business_schema.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/local_business_schema.html) — LocalBusiness JSON-LD for the public pages
    - [`sticky_cta.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/sticky_cta.html) — mobile call/contact bar for anonymous visitors

  </details>

</details>

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/static"><code>static/</code></a> — CSS, client-side JS, images, and web-accessible uploads</summary>

  - [`js/dxf_viewer.js`](https://github.com/protoknight12/Trafcom_Website/blob/main/static/js/dxf_viewer.js) — canvas rendering of DXF geometry
  - [`js/inline_edit.js`](https://github.com/protoknight12/Trafcom_Website/blob/main/static/js/inline_edit.js) — front-end for inline content editing
  - [`css/style.css`](https://github.com/protoknight12/Trafcom_Website/blob/main/static/css/style.css)
  - [`img/`](https://github.com/protoknight12/Trafcom_Website/tree/main/static/img)
  - `uploads/products/` — persistent, web-accessible product images

</details>

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/migration"><code>migration/</code></a> — one-off migration/backfill/seed scripts (see below)</summary>

  - [`change_admin_password.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/change_admin_password.py)
  - `backfill_erp_numbers.py` (local-only, not in this repository)
  - [`backfill_service_machine_cards.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/backfill_service_machine_cards.py)
  - [`seed_real_machines.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/seed_real_machines.py)
  - `seed_test_data.py` (local-only, not in this repository)
  - ...plus the various `migrate_*.py` schema-change and `backfill_*.py`
    data-backfill scripts (Shelly device/reading tables, the services
    pricing refactor, ...) —
    [browse the full folder](https://github.com/protoknight12/Trafcom_Website/tree/main/migration)

</details>

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/testing"><code>testing/</code></a> — test scripts (see below)</summary>

  - [`test_label_barcode.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_label_barcode.py)
  - [`test_cnc_pricing_engine.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_cnc_pricing_engine.py)
  - [`test_sheet_dimensions.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_sheet_dimensions.py)
  - [`test_rate_limiting.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_rate_limiting.py)
  - [`test_delivery_note_stock.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_delivery_note_stock.py)
  - [`test_delivery_note_matching.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_delivery_note_matching.py)
  - [`test_web_designer_role.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_web_designer_role.py)
  - [`test_eik_validation.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_eik_validation.py)
  - [`test_service_sections_grouping.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_service_sections_grouping.py)
  - [`test_material_option_format.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_material_option_format.py)
  - [`test_security_fixes.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_security_fixes.py)
  - [`test_quick_create_material.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_quick_create_material.py)
  - [`test_quick_create_product_components.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_quick_create_product_components.py)
  - [`test_detail_pdf_upload.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_detail_pdf_upload.py)
  - [`test_operation_description.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_operation_description.py)
  - [`test_order_item_operations.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_order_item_operations.py)
  - [`test_order_item_attachment.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_order_item_attachment.py)
  - [`test_shelly_status.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_status.py)
  - [`test_shelly_history_aggregation.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_history_aggregation.py)
  - [`test_shelly_device_routes.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_device_routes.py)
  - [`test_shelly_reading_log.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_reading_log.py)
  - [`test_admin_power_history_gen1.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_admin_power_history_gen1.py)
  - [`test_upload_and_machine_helpers.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_upload_and_machine_helpers.py)
  - [`test_material_only_detail_pricing.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_material_only_detail_pricing.py)
  - [`test_material_cost_by_type.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_material_cost_by_type.py)
  - [`test_length_based_operation_cost.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_length_based_operation_cost.py)
  - [`test_operations_breakdown_excludes_cutting.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_operations_breakdown_excludes_cutting.py)
  - [`test_operations_form_novalidate.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_operations_form_novalidate.py)
  - [`test_production_orders.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_production_orders.py)
  - [`test_production_order_clamp.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_production_order_clamp.py)
  - [`test_production_order_delete.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_production_order_delete.py)
  - [`test_production_stock_bump.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_production_stock_bump.py)
  - [`test_material_requests.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_material_requests.py)
  - [`test_offer_generator.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_offer_generator.py)
  - [`test_offer_create_order.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_offer_create_order.py)
  - [`test_detail_dxf_cyrillic.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_detail_dxf_cyrillic.py)
  - [`test_polyline_entity.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_polyline_entity.py)
  - [`test_generator_dxf_export.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_generator_dxf_export.py)
  - [`test_generator_presets.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_generator_presets.py)
  - [`test_storage_materials_thresholds.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_storage_materials_thresholds.py)
  - [`test_delivery_note_price_split.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_delivery_note_price_split.py)
  - [`test_service_show_price.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_service_show_price.py)
  - [`test_activity_log.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_activity_log.py)
  - [`test_registration_lock.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_registration_lock.py)
  - [`test_auth_email_2fa.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_auth_email_2fa.py)
  - [`test_email_verification.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_email_verification.py)
  - [`test_email_validation.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_email_validation.py)
  - [`test_error_pages_and_session.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_error_pages_and_session.py)

</details>

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/docs"><code>docs/</code></a> — reference docs</summary>

  - [`SHELLY_API.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/SHELLY_API.md) — function-by-function reference for the Shelly power-monitoring integration
  - [`shelly_dobavyane_masina_BG.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/shelly_dobavyane_masina_BG.md) — Bulgarian how-to for adding a machine on `/admin/power`

</details>

</details>

## Schema changes

`db.create_all()` only creates tables that don't exist yet — it never
alters existing ones (there's no Alembic in this project). Any model field
change needs either a fresh dev database or a hand-written migration script
in `migration/`, run as a module from the repo root, e.g.:

```bash
python -m migration.migrate_add_supplier_vat
```

See each script's module docstring for what it does and when to run it.
On a deployed server, `deploy/update.sh` runs every new `migration/migrate_*.py`
exactly once through `python -m migration.run_once` (see [Deployment](#deployment)).

## Tests

```bash
pip install -r requirements-dev.txt   # installs pytest
python -m testing.run_all
```

`run_all` runs the whole suite: every `testing/test_*.py` file in its own
process, so each file gets its own app and database. It exits non-zero if any
file fails or times out. Pass part of a file name to run a subset
(`python -m testing.run_all shelly`), and `-j N` sets how many files run in
parallel. Most files are plain `assert` scripts (they pass if they finish
without error); the request/response tests are pytest files.

To run one file on its own:

```bash
python -m pytest testing/test_security_fixes.py -v
python -m testing.test_label_barcode     # a plain assert script
```

Tests always use a throwaway SQLite database: `testing/__init__.py` points
`DATABASE_URL` at a temporary file before `app` is imported, and the root
`conftest.py` aborts any run whose database is not SQLite. They never touch
the database in `.env`.

GitHub Actions runs the whole suite on Python 3.12 on every push to `main`
and on every pull request ([`.github/workflows/tests.yml`](https://github.com/protoknight12/Trafcom_Website/blob/main/.github/workflows/tests.yml)).

## Configuration notes

- Rate limiting (`flask-limiter`) defaults to 300 requests/hour per client IP,
  counted separately for each route, with tighter limits on `/login` and `/register`. The app trusts
  one reverse-proxy hop (nginx) for the client address (`ProxyFix`, `x_for=1`).
  Storage is in-memory —
  per-process only (production runs a single worker, so that is fine there).
  If running more than one gunicorn/waitress worker,
  switch to a shared store (e.g. Redis) or limits won't be enforced
  correctly across workers.
- All state-changing requests require a CSRF token (`flask-wtf`).
- Shelly energy meters are managed from the `/admin/power` dashboard itself
  (add/rename/delete/relink a machine there, no restart needed). The
  `SHELLY_DEVICES` env var in `.env.example` is legacy, one-time-use only —
  it's read once on first startup after upgrading, to migrate whatever was
  configured there into the database; leave it blank on a fresh install. See
  [`docs/SHELLY_API.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/SHELLY_API.md).
