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
- [Project structure](#project-structure)
- [Schema changes](#schema-changes)
- [Tests](#tests)
- [Configuration notes](#configuration-notes)

## Features

- **DXF pricing** — upload a DXF file, get area/cut-length/pierce-count
  extracted from the geometry and priced against per-material rates plus a
  flat setup fee.
- **Catalog** — admin-curated `Detail` (single part) and `Product`
  (assembled from multiple Details + extra costs like paint/assembly, with
  markup) records, each with stock tracking and a printable barcode label.
- **Orders & production tracking** — customer orders (`Order` /
  `OrderItem`) roll up completion status per-component as production
  reports units produced, driven off frozen recipe snapshots so later
  catalog edits don't retroactively change placed orders.
- **Delivery notes / stock intake** — recording goods received from a
  `Supplier` bumps stock on the referenced material/detail/product.
- **Clients & deliverers** — lightweight lookup catalogs (with Bulgarian
  company legal fields — ЕИК, ДДС №, address, МОЛ) an order can reference.
- **Label printing** — Code128 barcode labels (rendered as inline SVG, no
  external service) for any catalog entry or produced batch.
- **Role-based access** — `regular_user`, `worker`, `admin`, `web_designer`,
  with a scoped-down content editor for non-pricing public-page text.
- **Offer / protocol / certificate documents** — browser-print pages
  (Ctrl+P to PDF), no server-side PDF library.
- **Missing-stock dashboard** — every open order that doesn't currently have
  enough Detail/Product stock to fulfill it, recomputed live rather than
  stored.
- **DXF revision history per Detail** — the original uploaded `.dxf` files
  behind a catalog Detail are kept and downloadable (admins only), not just
  the geometry extracted from them.
- **Live power monitoring** — a real-time dashboard (`/admin/power`)
  polling Shelly energy meters on the shop LAN (both Gen1 and Gen2 devices
  supported) with historical charts; see
  [`docs/SHELLY_API.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/SHELLY_API.md).

## Requirements

- Python 3
- PostgreSQL (reachable via `DATABASE_URL`)

## Installation

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in real values:

```
SECRET_KEY=
DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/cnc_calculator_db
FLASK_DEBUG=0
```

`SECRET_KEY` and `DATABASE_URL` are required — the app raises an error on
import if either is missing. `.env` is loaded automatically via
`python-dotenv` and is gitignored.

## Running it

```bash
python app.py
```

This also creates any missing database tables (`db.create_all()` — it does
**not** migrate existing ones, see [Schema changes](#schema-changes) below)
and seeds default material prices and machine cards on first run, then
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

## Project structure

<details>
<summary>Click to expand</summary>

- [`app.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/app.py) — models, DXF geometry/pricing logic, and all routes (single file)
- [`wsgi.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/wsgi.py) — production entrypoint (gunicorn/waitress)
- [`requirements.txt`](https://github.com/protoknight12/Trafcom_Website/blob/main/requirements.txt)
- [`.env.example`](https://github.com/protoknight12/Trafcom_Website/blob/main/.env.example)
- `uploads/` — private scratch folder for in-flight DXF uploads (not on GitHub — gitignored)

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/templates"><code>templates/</code></a> — Jinja2 templates, one per page/route</summary>

  - [`about.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/about.html)
  - [`admin.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin.html)
  - [`admin_clients.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_clients.html)
  - [`admin_delivery_notes.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_delivery_notes.html)
  - [`admin_details.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_details.html)
  - [`admin_materials.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_materials.html)
  - [`admin_missing_stock.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_missing_stock.html)
  - [`admin_power.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_power.html)
  - [`admin_products.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_products.html)
  - [`admin_users.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/admin_users.html)
  - [`certificate.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/certificate.html)
  - [`contact.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/contact.html)
  - [`content_editor.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/content_editor.html)
  - [`dashboard.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/dashboard.html)
  - [`detail_dxf_dashboard.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/detail_dxf_dashboard.html) — per-Detail DXF revision history
  - [`edit_window.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/edit_window.html)
  - [`generator.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/generator.html)
  - [`index.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/index.html)
  - [`label.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/label.html)
  - [`login.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/login.html)
  - [`machines.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/machines.html)
  - [`my_orders.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/my_orders.html)
  - [`offer.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/offer.html)
  - [`order_create.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/order_create.html)
  - [`product_edit.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/product_edit.html)
  - [`production_report.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/production_report.html)
  - [`protocol.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/protocol.html)
  - [`register.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/register.html)
  - [`services.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/services.html)
  - [`upload.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/upload.html)

  <details>
  <summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/templates/partials"><code>partials/</code></a> — shared chrome (navbar, footer, CSRF field, editable-text blocks)</summary>

    - [`navbar.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/navbar.html)
    - [`footer.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/footer.html)
    - [`editable.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/editable.html)
    - [`csrf_field.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/csrf_field.html)
    - [`material_options.html`](https://github.com/protoknight12/Trafcom_Website/blob/main/templates/partials/material_options.html)

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
  - [`backfill_erp_numbers.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/backfill_erp_numbers.py)
  - [`backfill_service_machine_cards.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/backfill_service_machine_cards.py)
  - [`seed_real_machines.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/seed_real_machines.py)
  - [`seed_test_data.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/migration/seed_test_data.py)
  - ...plus the various `migrate_*.py` schema-change scripts (including the
    Shelly `shelly_device_machine` many-to-many tables) —
    [browse the full folder](https://github.com/protoknight12/Trafcom_Website/tree/main/migration)

</details>

<details>
<summary><a href="https://github.com/protoknight12/Trafcom_Website/tree/main/testing"><code>testing/</code></a> — test scripts (see below)</summary>

  - [`test_label_barcode.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_label_barcode.py)
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
  - [`test_shelly_status.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_status.py)
  - [`test_shelly_history_aggregation.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_history_aggregation.py)
  - [`test_shelly_device_routes.py`](https://github.com/protoknight12/Trafcom_Website/blob/main/testing/test_shelly_device_routes.py)

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

## Tests

Most tests are plain `assert`-based scripts, run as modules from the repo
root, e.g.:

```bash
python -m testing.test_label_barcode
```

A few use pytest instead, where the thing being tested needs real
request/response behavior (redirects, status codes, session auth):

```bash
pip install pytest flask-wtf
pytest testing/test_security_fixes.py -v
```

## Configuration notes

- Rate limiting (`flask-limiter`) defaults to 300 requests/hour per IP,
  with tighter limits on `/login` and `/register`. Storage is in-memory —
  per-process only. If running more than one gunicorn/waitress worker,
  switch to a shared store (e.g. Redis) or limits won't be enforced
  correctly across workers.
- All state-changing requests require a CSRF token (`flask-wtf`).
- Shelly energy meters are managed from the `/admin/power` dashboard itself
  (add/rename/delete/relink a machine there, no restart needed). The
  `SHELLY_DEVICES` env var in `.env.example` is legacy, one-time-use only —
  it's read once on first startup after upgrading, to migrate whatever was
  configured there into the database; leave it blank on a fresh install. See
  [`docs/SHELLY_API.md`](https://github.com/protoknight12/Trafcom_Website/blob/main/docs/SHELLY_API.md).
