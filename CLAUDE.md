# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Flask app for a CNC/laser cutting shop (Trafcom): upload a DXF drawing, get its cutting
price calculated automatically from geometry (area, cut length, pierce count), build a
catalog of reusable "Details" and "Products" from priced drawings, and run customer orders
through production tracking. UI strings and flash messages are in Bulgarian.

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Dependencies are pinned-free in `requirements.txt`: `flask`, `flask-sqlalchemy`, `flask-login`,
`flask-limiter` (rate limiting), `werkzeug`, `ezdxf`, `psycopg2-binary`, `python-barcode`
(label printing, see `print_label()`), `python-dotenv`.

### Config & secrets

`SECRET_KEY` and `DATABASE_URL` are **required** environment variables — `app.py` raises a
`RuntimeError` on import if either is missing. There is no hardcoded fallback (there used to
be one with a real DB password committed in source; that's gone now — see git history if you
need to know why this is enforced so strictly). Copy `.env.example` to `.env` and fill in real
values for local dev; `python-dotenv` auto-loads `.env` if present. `.env` itself is
gitignored — never commit it.

- Requires PostgreSQL reachable via `DATABASE_URL`.
- On startup, `db.create_all()` creates missing tables (does **not** migrate/alter existing
  ones — there's no Alembic in this project), seeds `MaterialPrice` defaults, and creates an
  `admin` / `admin123` user if none exists. **This only happens inside the
  `if __name__ == '__main__':` block at the bottom of `app.py`** — a production WSGI server
  (gunicorn, waitress) imports `app` as a callable and never executes that block. Run
  `python app.py` once by hand to initialize the DB before switching to a WSGI server for real
  serving.
- Opens the browser to `http://127.0.0.1:5000/` automatically when run directly. Debug mode is
  **off by default** — set `FLASK_DEBUG=1` in your environment for local dev (interactive
  debugger, auto-reload). Never set it in a public deployment.
- Rate limiting (`flask-limiter`) is on: a global default of 300 req/hour per IP, plus
  `/login` (10/min) and `/register` (5/hour) specifically, to blunt brute-force and spam
  signups. Storage is in-memory — per-process only, resets on restart, and **not** shared
  across multiple worker processes. If you ever run this with more than one gunicorn/waitress
  worker, switch `Limiter(..., storage_uri="redis://...")` or the limits silently stop being
  enforced correctly.
- No linter or build step exists in this repo. Tests are plain `assert`-based scripts, no
  framework — see [Tests](#tests) below.

### Schema changes

Because `db.create_all()` never alters existing tables, any model field change requires
either dropping/recreating the affected table(s) in dev, or a hand-written migration. There's
a note about this at the bottom of `app.py` near `db.create_all()` — read it before changing
any model.

The repo already has several one-off migration scripts (`migrate_add_detail_label_fields.py`,
`migrate_add_product_material_label_fields.py`, `migrate_add_sheet_dimensions.py`,
`migrate_erp_number_unique_int.py`) plus a backfill (`backfill_erp_numbers.py`) — these exist
to bring an *existing* pre-ERP-number/label database up to the current schema, run in that
order. **A brand-new empty database doesn't need any of them** — `db.create_all()` already
creates the current schema directly. Follow this same pattern (a standalone `migrate_*.py`
using raw `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, safe to re-run) for future model changes
instead of introducing Alembic.

## Architecture

Everything server-side lives in one file: **`app.py`** (~2000 lines: models, DXF geometry/
pricing logic, and all routes, in that order). There is no blueprint/package split — when
adding routes, follow the existing single-file convention rather than introducing one.

- `templates/` — Jinja2 templates, one per page/route. `templates/partials/navbar.html` is
  shared chrome. `templates/label.html` is the print-label page (see ERP numbers section
  below) — it's a real dependency of `print_label()`, don't let it go untracked in git.
- `static/js/dxf_viewer.js` — canvas rendering of the JSON shape list produced by
  `analyze_dxf_geometry()` (used by both the dashboard upload viewer and elsewhere). Only
  emits/handles `line`/`circle`/`arc` shapes — polylines are decomposed into those before
  reaching this layer, so don't add a `polyline` shape type back in without also updating
  `process_entity()`.
- `uploads/` — private scratch folder for in-flight DXF uploads (deleted after processing,
  see `process_dxf_upload`). Not the same as `static/uploads/products/`, which holds
  persistent product images and *is* web-accessible. Both are gitignored and auto-created via
  `os.makedirs(..., exist_ok=True)` on startup.
- `.env` / `.env.example`, `.gitignore`, `requirements.txt` — deployment/config plumbing, see
  [Config & secrets](#config--secrets) above.

### Domain model chain

```
DxfFile   — a user's raw upload + its calculated price (personal history, per-user)
Detail    — an admin-curated catalog part, built the same way as a DxfFile (DXF + material
            → geometry → price) but independent of any user's upload history
Product   — assembled from N Details (via ProductDetail, with quantity) + ProductExtraCost
            line items (paint, assembly, transport...) + a markup_percent
Order     — a customer's cart: OrderItems, each either a whole Product or a standalone Detail
```

Key invariant: pricing and geometry extraction for a `DxfFile` and a `Detail` **must** go
through the same functions (`analyze_dxf_geometry` → `calculate_cnc_price`) — don't
special-case one path.

### Production tracking

Progress is tracked at the *Detail* level, not the order-item level. When an order is placed,
each product `OrderItem` gets a frozen snapshot per component (`OrderItemComponent`, one row
per Detail in that product's recipe at that moment) — later edits to a product's recipe never
retroactively change an already-placed order. `quantity_produced` is entered by admins per
component; `OrderItem.percent_complete` and `Order.percent_complete` roll this up (weighted by
raw detail-piece units, not by line-item count). `refresh_order_status()` derives the order's
`new`/`in_production`/`completed` status from this rollup — `cancelled` is the only status set
manually and is never overwritten by it.

Order `status` is stored as an ASCII slug (`in_production`, not the Bulgarian label) — see
`STATUS_LABELS` for the display mapping. This matters because the slug is interpolated
directly into a CSS class name in templates.

### DXF geometry → price pipeline

1. `process_entity()` — reads one DXF entity (LINE/CIRCLE/ARC/LWPOLYLINE/POLYLINE, including
   bulge-arc decomposition for rounded polyline corners) and in a single pass returns its
   cutting length, endpoint segments, and drawable shape(s). Deliberately combined into one
   pass instead of three separate traversals.
2. `count_pierces()` — BFS over segment endpoints (within a tolerance) to count connected
   components = number of separate closed loops/paths the head must pierce. O(n²); fine for
   normal part complexity.
3. `compute_bounding_box()` — outer width/height computed from the *same* sanitized shape list
   used for length/pierces/viewer, not a separate raw ezdxf bbox call, so price and what's
   drawn can never silently disagree.
4. `calculate_cnc_price()` — converts width/height/length from mm to m²/m, applies the
   material's `cost_per_m2` / `cost_per_meter_cut` / `cost_per_pierce`, adds the flat
   `BASE_SETUP_FEE`.

`MaterialPrice` rows are seeded once from `DEFAULT_MATERIAL_SEED` and then live entirely in
the DB — admins edit them at runtime; the seed dict is never read again after first run.

### Auth

`flask_login` session auth. Three roles on `User.role`: `regular_user`, `worker`, `admin`
(`is_staff` = worker or admin). Route protection is via `@login_required` (any logged-in user)
or `@role_required('admin')` / `@role_required(['admin', 'worker'])` for role-gated routes —
use the existing decorator rather than checking `current_user.role` inline in new routes.

### Offer / protocol / certificate documents

`offer.html`, `protocol.html`, `certificate.html` are browser-print documents (`@media print`
CSS, no server-side PDF library) — the user hits Ctrl+P / "print to PDF" in the browser.

### ERP numbers & label printing

`Detail`, `Product`, and `MaterialPrice` each carry an `erp_number` (unique `Integer`,
auto-generated via `_next_erp_number()` if left blank on create/edit — one past the current
max across all three tables combined) and a free-text `code_number` ("КД №"). Uniqueness is
enforced *across all three tables together*, not per-table, via `_erp_number_conflict()` — a
scanned/typed ERP № must resolve to exactly one record (`erp_lookup()`).

`print_label()` renders `label.html` for either a catalog entry on its own (`detail` /
`product` / `material`, quantity from a `?quantity=` query param) or a produced batch on an
order (`item` / `component`, quantity from `quantity_produced`) — same `target_type`/
`target_id` convention used by `admin_production_report()`'s POST handler. The barcode itself
is Code128, rendered inline as SVG by `generate_barcode_svg()` (`python-barcode`) — no CDN JS
barcode library. **`erp_number` must be passed as a string, not an int** — this broke in
production once already (see `test_label_barcode.py`).

`update_label_codes()` is a quick-edit for ERP №/КД № directly from the label print page, so
you don't have to leave it to go fix a missing code in the admin panel first.

### Tests

Plain `assert`-based scripts, no framework/fixtures — run each directly, e.g.
`python test_label_barcode.py`. Each one guards a specific non-trivial/regression-prone piece
of logic rather than aiming for coverage:

- `test_sheet_dimensions.py` — `_parse_sheet_dimensions()` branch coverage (blank/valid/
  partial/negative/non-numeric input).
- `test_label_barcode.py` — `generate_barcode_svg()`; guards the int-vs-str regression noted
  above.
- `test_rate_limiting.py` — floods `GET /login` past its per-minute cap and asserts a 429
  shows up; guards the `@limiter.limit(...)` decorator on `login()` actually being present and
  working.

`seed_test_data.py` and `backfill_erp_numbers.py` are one-off dev-data scripts, not tests —
see their module docstrings for what they do and when to run them.
