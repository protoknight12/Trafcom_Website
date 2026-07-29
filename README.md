# Trafcom DXF Calculate

A Flask app for a CNC/laser cutting company (Trafcom): upload a DXF drawing and
get its cutting price calculated automatically from geometry (area, cut
length, pierce count), build a catalog of reusable "Details" and "Products"
from priced drawings, track stock via delivery notes, and run customer
orders through production tracking. The UI is in Bulgarian.

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

```
app.py               — models, DXF geometry/pricing logic, and all routes (single file)
wsgi.py               — production entrypoint (gunicorn/waitress)
templates/            — Jinja2 templates, one per page/route
static/js/             — dxf_viewer.js (canvas rendering), inline_edit.js (content editing)
migration/             — one-off migration/backfill/seed scripts (see below)
testing/               — test scripts (see below)
uploads/                — private scratch folder for in-flight DXF uploads
static/uploads/products/ — persistent, web-accessible product images
```

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
