from datetime import datetime
import os
import json
import math
import re
import uuid
import io
import webbrowser
import threading
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox
from ezdxf.math import bulge_to_arc
import random
import barcode
from barcode.writer import SVGWriter

# Optional: load a local .env file if python-dotenv is installed, so secrets
# can be kept out of source control. Safe no-op if the package isn't present.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# ----------------- APP CONFIGURATION -----------------
# No hardcoded fallbacks - both must come from the environment (.env locally,
# real env vars in deployment) so secrets never live in source control.
try:
    app.config['SECRET_KEY'] = os.environ['SECRET_KEY']
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
except KeyError as e:
    raise RuntimeError(
        f'Missing required environment variable: {e.args[0]}. '
        'Set SECRET_KEY and DATABASE_URL (e.g. in a .env file - see .env.example).'
    ) from e
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
# Belt-and-suspenders alongside CSRFProtect below: without an explicit
# SameSite, the session cookie's cross-site behavior is left to each
# browser's own default rather than a value this app controls.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# All state-changing (POST/PUT/PATCH/DELETE) requests must carry a valid CSRF
# token - either the `csrf_token` form field or an `X-CSRFToken` header for
# AJAX calls - or they're rejected with 400 before the view function runs.
csrf = CSRFProtect(app)

# Flat fee added to every job to cover machine setup/initialization overhead.
BASE_SETUP_FEE = 5.00

# Order status is stored as a plain-ASCII slug (safe to use directly as a CSS
# class, e.g. "status-in_production") and displayed via STATUS_LABELS. The
# old code stored the Bulgarian label itself (e.g. "В производство") as the
# status value, which broke when interpolated into class="status-{{ status }}"
# because the space split it into two separate CSS classes.
STATUS_LABELS = {
    'new': 'Нова',
    'in_production': 'В производство',
    'completed': 'Завършена',
    'cancelled': 'Отменена',
}

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ponytail: in-memory storage - per-process only, resets on restart and
# won't be shared across multiple gunicorn/waitress workers. Switch to
# storage_uri="redis://..." if this ever runs with >1 worker process.
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per hour"])

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['PRODUCT_IMAGES_FOLDER'] = os.path.join(app.static_folder, 'uploads', 'products')
os.makedirs(app.config['PRODUCT_IMAGES_FOLDER'], exist_ok=True)
# Same folder the seeded machine cards' images already live in (see
# SERVICE_MACHINE_CARDS_SEED/INDEX_MACHINE_CARDS_SEED) - admin-uploaded
# machine images join them here.
app.config['MACHINE_IMAGES_FOLDER'] = os.path.join(app.static_folder, 'img', 'machines')
os.makedirs(app.config['MACHINE_IMAGES_FOLDER'], exist_ok=True)


# ----------------- МОДЕЛИ В БАЗАТА ДАННИ -----------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    # roles: 'regular_user', 'worker', 'admin', 'web_designer'
    role = db.Column(db.String(20), default='regular_user')

    uploads = db.relationship('DxfFile', cascade='all, delete-orphan', backref='owner', lazy=True)

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_worker(self):
        return self.role == 'worker'

    @property
    def is_staff(self):
        """Admins and workers both have production/machine-floor access."""
        return self.role in ('admin', 'worker')

    @property
    def can_edit_content(self):
        """Admins and web designers can redact info pages (machine names, detail names, product text)."""
        return self.role in ('admin', 'web_designer')


class DxfFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(150), nullable=False)
    material = db.Column(db.String(50), nullable=False)
    width = db.Column(db.Float, nullable=False)
    height = db.Column(db.Float, nullable=False)
    total_length = db.Column(db.Float, nullable=False)
    calculated_price = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # Stores the extracted 2D geometry (lines/arcs/circles) as a JSON string,
    # so the viewer modal can render the drawing without re-parsing the DXF file.
    geometry_json = db.Column(db.Text, nullable=True)
    machine_id = db.Column(db.Integer, db.ForeignKey('machine.id'), nullable=True)
    machine = db.relationship('Machine', backref='dxf_files')


class MaterialPrice(db.Model):
    """
    Per-material pricing, editable by admins at runtime instead of being
    hardcoded in source. `key` is the stable internal identifier used in
    DxfFile.material and the dashboard's material <select> - it's
    auto-generated when a material is created, not edited through the UI.

    Prices are stored in human-friendly units (EUR per square meter, EUR per
    meter of cut) rather than per mm2/per mm - the raw per-mm values needed
    for typical prices are tiny (e.g. 0.00001), which is awkward to enter and
    read for non-technical staff. calculate_cnc_price() converts the
    drawing's mm-based measurements into m2/m before applying these rates, so
    the actual calculated price is unaffected by this unit choice - only
    what admins type/see changes.
    """
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    cost_per_m2 = db.Column(db.Float, nullable=False)
    cost_per_meter_cut = db.Column(db.Float, nullable=False)
    cost_per_pierce = db.Column(db.Float, nullable=False)
    # Standard stock sheet size this material entry represents (mm). Purely
    # informational catalog data - pricing still runs off the cut part's own
    # geometry (calculate_cnc_price), not off these. Optional/nullable since
    # older rows and non-sheet materials won't have them.
    sheet_length_mm = db.Column(db.Float, nullable=True)
    sheet_width_mm = db.Column(db.Float, nullable=True)
    thickness_mm = db.Column(db.Float, nullable=True)
    # ERP code (shown as text + Code128 barcode) and internal part code (КД №)
    # printed on production labels - see print_label(). Optional/nullable
    # since older rows won't have them.
    erp_number = db.Column(db.Integer, nullable=True, unique=True)
    code_number = db.Column(db.String(100), nullable=True)
    # Structural form of the stock (sheet/rod/profile/pipe) - see
    # MATERIAL_TYPE_LABELS. Existing rows are all sheet stock, so this
    # defaults to 'sheets' rather than forcing every material select
    # throughout the app to handle a blank/unknown group.
    type = db.Column(db.String(30), nullable=False, default='sheets')
    # Free-text manufacturer/variant tag (e.g. "Alcoa", "DC01") - purely
    # informational, shown alongside display_name where relevant.
    brand = db.Column(db.String(100), nullable=True)
    # Stock on hand, bumped by recording delivery notes (see DeliveryNoteItem
    # / admin_delivery_notes.html) - not editable by hand elsewhere.
    stock_quantity = db.Column(db.Float, nullable=False, default=0.0)


# Structural-form categories a material's stock can come in, driving the
# <optgroup> grouping on every material <select> in the app (see
# partials/material_options.html) and the type dropdown on the admin
# materials page. Not a DB table - this is a fixed, small set of physical
# stock forms, not admin-editable data.
MATERIAL_TYPE_LABELS = {
    'sheets': 'Листове/Плочи',
    'rods': 'Пръти',
    'profiles': 'Профили',
    'pipes': 'Тръби',
    'other': 'Други',
}


def _validate_eik(raw):
    """
    ЕИК/Булстат is optional everywhere it appears, but when provided must be
    exactly 9 digits - not the old 9-or-13-digit Bulstat format, per explicit
    business rule. Returns (cleaned_value_or_None, error_message_or_None).
    """
    value = (raw or '').strip()
    if not value:
        return None, None
    if not re.fullmatch(r'\d{9}', value):
        return None, 'ЕИК трябва да съдържа точно 9 цифри.'
    return value, None


class Client(db.Model):
    """
    A customer an order can be placed for. Only `name` is required - it
    doubles as the company name when client_type == 'company' (no separate
    company_name column). The legal-entity fields (eik/vat_number/address/
    mol) are optional/blank for individuals and only meaningful once
    client_type is switched to 'company' in the admin UI.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    client_type = db.Column(db.String(20), nullable=False, default='individual')  # 'individual' or 'company'
    eik = db.Column(db.String(20), nullable=True)  # ЕИК / Булстат
    vat_number = db.Column(db.String(20), nullable=True)  # ИН по ДДС
    address = db.Column(db.String(255), nullable=True)  # Адрес на управление
    mol = db.Column(db.String(150), nullable=True)  # МОЛ - материално отговорно лице


class Deliverer(db.Model):
    """
    A delivery provider (куриер) an order can be shipped through. Same
    legal-entity pattern as Client - `name` doubles as the company name, the
    fields below are optional/blank unless the courier is a registered
    company.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    eik = db.Column(db.String(20), nullable=True)
    vat_number = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    mol = db.Column(db.String(150), nullable=True)


class Detail(db.Model):
    """
    A reusable, admin-curated catalog component ("детайл") - built once from
    a DXF upload + material choice (using the exact same geometry/pricing
    logic as the main calculator), then reused across any number of
    Products. Deliberately NOT tied to a specific user's personal upload
    library (DxfFile) - that's per-user upload history, this is a shared
    parts catalog admins maintain independently.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    material_key = db.Column(db.String(50), db.ForeignKey('material_price.key'), nullable=False)
    width = db.Column(db.Float, nullable=False)
    height = db.Column(db.Float, nullable=False)
    total_length = db.Column(db.Float, nullable=False)
    pierce_count = db.Column(db.Integer, nullable=False)
    calculated_price = db.Column(db.Float, nullable=False)
    geometry_json = db.Column(db.Text, nullable=True)
    # ERP code (shown as text + Code128 barcode) and internal part code (КД №)
    # printed on production labels - see print_label(). Optional/nullable
    # since older catalog parts won't have these set.
    erp_number = db.Column(db.Integer, nullable=True, unique=True)
    code_number = db.Column(db.String(100), nullable=True)
    # Stock on hand, bumped by recording delivery notes (see DeliveryNoteItem
    # / admin_delivery_notes.html) - not editable by hand elsewhere.
    stock_quantity = db.Column(db.Float, nullable=False, default=0.0)

    material = db.relationship('MaterialPrice')


class ProductImage(db.Model):
    """Stores references to uploaded persistent product documentation or marketing images."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)


class Product(db.Model):
    """
    A sellable product assembled from one or more Details (with quantities)
    plus optional extra costs (painting, assembly, transport, etc.) and an
    optional markup percentage applied on top of total cost to get the
    actual sell price shown on generated offers.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    markup_percent = db.Column(db.Float, nullable=False, default=0.0)
    # ERP code (shown as text + Code128 barcode) and internal part code (КД №)
    # printed on production labels - see print_label(). Optional/nullable
    # since older rows won't have them.
    erp_number = db.Column(db.Integer, nullable=True, unique=True)
    code_number = db.Column(db.String(100), nullable=True)
    # Stock on hand, bumped by recording delivery notes (see DeliveryNoteItem
    # / admin_delivery_notes.html) - not editable by hand elsewhere.
    stock_quantity = db.Column(db.Float, nullable=False, default=0.0)

    product_details = db.relationship('ProductDetail', cascade='all, delete-orphan', backref='product', lazy=True)
    extra_costs = db.relationship('ProductExtraCost', cascade='all, delete-orphan', backref='product', lazy=True)
    # ADD THIS RELATIONSHIP BINDING:
    images = db.relationship('ProductImage', cascade='all, delete-orphan', backref='product', lazy=True)


class ProductDetail(db.Model):
    """Join table: which Details compose a Product, and in what quantity."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    detail = db.relationship('Detail')


class Order(db.Model):
    """
    A customer order, placed by a logged-in user. Cart-style: one Order can
    contain any number of OrderItems, each either a whole Product or a
    standalone Detail.

    Completion percentage and status are NOT tracked per-product - they're
    derived from the individual Detail components a product order line is
    made of (see OrderItemComponent), so a product is only "done" once every
    one of its constituent details has actually been produced. See
    Order.percent_complete / OrderItem.percent_complete below.
    """
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    customer_name = db.Column(db.String(150), nullable=False)
    status = db.Column(db.String(50), default='new')  # new, in_production, completed, cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    machine_id = db.Column(db.Integer, db.ForeignKey('machine.id'), nullable=True)
    # Optional links to the Client/Deliverer catalogs (see order_create.html's
    # select-or-quick-create UI). customer_name stays the field actually
    # displayed everywhere (my_orders.html, production_report.html, offer/
    # protocol/certificate) - it's auto-filled from the selected/created
    # client's name, so none of those templates need to change.
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    deliverer_id = db.Column(db.Integer, db.ForeignKey('deliverer.id'), nullable=True)

    user = db.relationship('User', backref=db.backref('orders', lazy=True))
    machine = db.relationship('Machine', backref='orders')
    client = db.relationship('Client')
    deliverer = db.relationship('Deliverer')
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade="all, delete-orphan")

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def total_price(self):
        return round(sum(item.line_total for item in self.items), 2)

    @property
    def percent_complete(self):
        """
        Weighted by raw detail-piece units across every item in the order
        (a product's own units aren't the unit of account - its components
        are), so an order half full of easy small parts and half full of a
        single complex product reflects genuine production progress rather
        than "1 of 2 line items done".
        """
        total_needed = 0
        total_produced = 0
        for item in self.items:
            needed, produced = item.detail_unit_totals
            total_needed += needed
            total_produced += produced
        if total_needed <= 0:
            return 0.0
        return round(total_produced / total_needed * 100, 1)

    @property
    def can_cancel(self):
        # Once any production has started (or it's already done/cancelled),
        # cancelling would discard real work - only a brand new order is
        # safe for a customer to cancel themselves.
        return self.status == 'new'


class OrderItem(db.Model):
    """
    One line in an Order: a quantity of either a whole Product or a
    standalone Detail. `unit_price` is snapshotted at order-creation time
    (from the product/detail's price at that moment) so a later price change
    never rewrites the cost of an order that's already been placed.

    For product line items, production progress is tracked per-component via
    OrderItemComponent (see below), NOT via quantity_produced on this row -
    that field is only meaningful for standalone-detail line items, which
    have no sub-components to track separately.
    """
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=True)
    quantity_ordered = db.Column(db.Integer, nullable=False)
    quantity_produced = db.Column(db.Integer, default=0, nullable=False)  # only used for standalone-detail items
    unit_price = db.Column(db.Float, nullable=False, default=0.0)

    product = db.relationship('Product')
    detail = db.relationship('Detail')

    @property
    def quantity_remaining(self):
        rem = self.quantity_ordered - self.quantity_produced
        return rem if rem > 0 else 0

    @property
    def item_name(self):
        if self.product:
            return self.product.name
        if self.detail:
            return self.detail.name
        return 'Неизвестен артикул'

    @property
    def line_total(self):
        return round(self.unit_price * self.quantity_ordered, 2)

    @property
    def detail_unit_totals(self):
        """
        (needed, produced) expressed in raw detail-piece units - used both
        for this item's own percent_complete and as this item's weighted
        contribution to the parent Order's percent_complete.
        """
        if self.product_id:
            needed = sum(c.quantity_needed for c in self.components)
            produced = sum(min(c.quantity_produced, c.quantity_needed) for c in self.components)
        else:
            needed = self.quantity_ordered
            produced = min(self.quantity_produced, self.quantity_ordered)
        return needed, produced

    @property
    def percent_complete(self):
        needed, produced = self.detail_unit_totals
        if needed <= 0:
            return 100.0
        return round(produced / needed * 100, 1)


class OrderItemComponent(db.Model):
    """
    A frozen snapshot of one Detail's production requirement for a single
    product OrderItem, created once when the order is placed (so later edits
    to a product's recipe never retroactively change an already-placed
    order). This is the actual unit of production tracking for product line
    items: quantity_produced is entered by admins per-component, and rolled
    up into OrderItem.percent_complete / Order.percent_complete.
    """
    id = db.Column(db.Integer, primary_key=True)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_item.id'), nullable=False)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=True)
    detail_name_snapshot = db.Column(db.String(150), nullable=False)
    quantity_needed = db.Column(db.Integer, nullable=False)
    quantity_produced = db.Column(db.Integer, default=0, nullable=False)

    order_item = db.relationship('OrderItem', backref=db.backref('components', cascade='all, delete-orphan', lazy=True))
    detail = db.relationship('Detail')

    @property
    def quantity_remaining(self):
        rem = self.quantity_needed - self.quantity_produced
        return rem if rem > 0 else 0

    @property
    def percent_complete(self):
        if self.quantity_needed <= 0:
            return 100.0
        return round(min(self.quantity_produced, self.quantity_needed) / self.quantity_needed * 100, 1)


class ProductExtraCost(db.Model):
    """
    A flexible named cost line item on a Product (e.g. "Боядисване" -> 50.00,
    "Монтаж" -> 30.00, "Транспорт" -> 20.00) - deliberately not a fixed set
    of columns, since these vary per product and per business need.
    """
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)


def calculate_product_pricing(product):
    """
    Returns a dict with the full cost/price breakdown for a product:
    details subtotal, extra costs subtotal, total cost, markup amount, and
    final sell price. Centralized here so the products list, edit page, and
    offer view can never disagree with each other.
    """
    details_subtotal = sum(pd.detail.calculated_price * pd.quantity for pd in product.product_details)
    extra_costs_subtotal = sum(ec.amount for ec in product.extra_costs)
    total_cost = details_subtotal + extra_costs_subtotal
    markup_amount = total_cost * (product.markup_percent / 100.0)
    sell_price = total_cost + markup_amount

    return {
        'details_subtotal': round(details_subtotal, 2),
        'extra_costs_subtotal': round(extra_costs_subtotal, 2),
        'total_cost': round(total_cost, 2),
        'markup_amount': round(markup_amount, 2),
        'sell_price': round(sell_price, 2),
    }


class Supplier(db.Model):
    """A goods supplier a DeliveryNote can be received from (e.g. "ЕХНАТОН БЪЛГАРИЯ АД")."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    eik = db.Column(db.String(20), nullable=True)
    vat_number = db.Column(db.String(20), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(150), nullable=True)


class DeliveryNote(db.Model):
    """
    A goods-received delivery note / invoice (фактура за доставка) entered by
    an admin/worker to bring stock into the system - see
    admin_delivery_notes.html. Recording one just bumps stock_quantity on
    each referenced MaterialPrice/Detail/Product line (via DeliveryNoteItem)
    and keeps a paper trail of what came in, from whom, and at what cost.
    """
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    note_number = db.Column(db.String(100), nullable=True)  # Фактура № / Наша реф.
    note_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    supplier = db.relationship('Supplier')
    created_by = db.relationship('User')
    items = db.relationship('DeliveryNoteItem', backref='delivery_note', cascade='all, delete-orphan', lazy=True)


class DeliveryNoteItem(db.Model):
    """
    One received line item on a DeliveryNote, pointing at whichever catalog
    row it restocks - same target_type/target_id convention already used by
    print_label()/erp_lookup() ('material' / 'detail' / 'product').
    description_snapshot freezes the item's name at intake time (matches
    OrderItemComponent.detail_name_snapshot), so the note stays legible even
    if the catalog row is later renamed or deleted.

    width/height/thickness/brand are pre-filled from the selected catalog
    row's own parameters (see admin_delivery_notes() *_data lists) but are
    editable per line before saving - a real paper delivery note can list a
    batch that differs slightly from the catalog default. notes is a free
    custom description on top of that, for anything else worth recording.
    """
    id = db.Column(db.Integer, primary_key=True)
    delivery_note_id = db.Column(db.Integer, db.ForeignKey('delivery_note.id'), nullable=False)
    target_type = db.Column(db.String(20), nullable=False)  # 'material' / 'detail' / 'product'
    target_id = db.Column(db.Integer, nullable=False)
    description_snapshot = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=True)
    notes = db.Column(db.String(255), nullable=True)
    width = db.Column(db.Float, nullable=True)
    height = db.Column(db.Float, nullable=True)
    thickness = db.Column(db.Float, nullable=True)
    brand = db.Column(db.String(100), nullable=True)


class Machine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), default='idle')  # idle, running, maintenance
    last_maintenance = db.Column(db.DateTime, default=datetime.utcnow)


class ServiceMachineCard(db.Model):
    """
    A machine card shown on the public services page or homepage (see services(),
    index(), services.html, index.html). Originally just title + description for
    web-designer-added cards; now also backs the migrated-from-hardcoded-HTML cards
    (see SERVICE_MACHINE_CARDS_SEED / INDEX_MACHINE_CARDS_SEED), which is why
    section_title/specs_text/image_filename exist - those three are display-only
    extras the content editor doesn't expose for *new* cards (keeps the "add a
    machine" popup simple), but are preserved/editable on migrated ones.
    page ('services' or 'index') scopes which page a card shows up on - the two
    pages curate different wording for some of the same physical machines, so they
    deliberately don't share rows. section_title groups cards into the services
    page's section headers (e.g. "ФРЕЗОВИ ЦЕНТРОВЕ"); unused on the homepage, which
    renders one flat grid. specs_text is free-form "Label: Value" lines.
    """
    id = db.Column(db.Integer, primary_key=True)
    page = db.Column(db.String(20), nullable=False, default='services')
    section_title = db.Column(db.String(150), nullable=True)
    series_label = db.Column(db.String(100), nullable=True)
    title = db.Column(db.String(150), nullable=False)
    specs_text = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def specs(self):
        """Parses specs_text ('Label: Value' per line) into [(label, value), ...] for rendering."""
        rows = []
        for line in (self.specs_text or '').splitlines():
            if ':' in line:
                label, _, value = line.partition(':')
                rows.append((label.strip(), value.strip()))
        return rows


class EditableText(db.Model):
    """
    Generic key/value store for wiki-style editable prose blocks on public pages
    (see get_text() and templates/partials/editable.html) - e.g. key
    'index.hero_lead'. Missing key = the template's hardcoded default is shown, so
    nothing needs seeding; a row only appears once someone actually edits that text.
    """
    key = db.Column(db.String(150), primary_key=True)
    content = db.Column(db.Text, nullable=False)


def get_text(key, default=''):
    row = db.session.get(EditableText, key)
    return row.content if row else default


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------- DXF ГЕОМЕТРИЯ И ЦЕНИ -----------------

# One-time seed data: used only to populate the MaterialPrice table on first
# run (see seed_material_prices() below). After that, prices are read from
# and edited through the database - NOT from this dict - so admins can
# change them at runtime via the admin panel without a code change/redeploy.
# These are the same real prices as before, just re-expressed in EUR/m2 and
# EUR/meter-of-cut instead of EUR/mm2 and EUR/mm - mathematically identical,
# just friendlier numbers (e.g. 0.00001 EUR/mm2 = 10.00 EUR/m2).
DEFAULT_MATERIAL_SEED = {
    "wood": {"cost_per_m2": 10.00, "cost_per_meter_cut": 0.80, "cost_per_pierce": 0.05,
             "name": "Дървесен материал / МДФ"},
    "steel": {"cost_per_m2": 20.00, "cost_per_meter_cut": 1.50, "cost_per_pierce": 0.15, "name": "Въглеродна стомана"},
    "stainless_steel": {"cost_per_m2": 50.00, "cost_per_meter_cut": 2.50, "cost_per_pierce": 0.25,
                        "name": "Неръждаема стомана"},
    "aluminum": {"cost_per_m2": 40.00, "cost_per_meter_cut": 2.00, "cost_per_pierce": 0.20, "name": "Алуминий"},
    "copper": {"cost_per_m2": 120.00, "cost_per_meter_cut": 4.00, "cost_per_pierce": 0.40, "name": "Мед"},
    "brass": {"cost_per_m2": 90.00, "cost_per_meter_cut": 3.50, "cost_per_pierce": 0.35, "name": "Месинг"},
    "galvanized": {"cost_per_m2": 30.00, "cost_per_meter_cut": 1.80, "cost_per_pierce": 0.18,
                   "name": "Поцинкована ламарина"}
}


def seed_material_prices():
    """
    Populates the MaterialPrice table from DEFAULT_MATERIAL_SEED, but only
    for keys that don't already exist - safe to call on every startup.
    Existing rows (including any prices an admin has already edited, or new
    materials an admin has added) are never overwritten.
    """
    for key, cfg in DEFAULT_MATERIAL_SEED.items():
        if not MaterialPrice.query.filter_by(key=key).first():
            db.session.add(MaterialPrice(
                key=key,
                display_name=cfg['name'],
                cost_per_m2=cfg['cost_per_m2'],
                cost_per_meter_cut=cfg['cost_per_meter_cut'],
                cost_per_pierce=cfg['cost_per_pierce']
            ))
    db.session.commit()


# One-time seed for the public services page's machine-park cards - was hardcoded
# HTML in services.html, now lives in ServiceMachineCard so web designers/admins can
# edit it. specs_text is "Label: Value" per line (see ServiceMachineCard.specs).
SERVICE_MACHINE_CARDS_SEED = [
    {"section_title": "ФРЕЗОВИ ЦЕНТРОВЕ", "series_label": "5-ОСНО ФРЕЗОВАНЕ", "title": "DMG MORI DMU 75 monoBLOCK",
     "image_filename": "dmg-mori-dmu75.jpg",
     "specs_text": "Обработваем диаметър: 750 - 650 мм\nХод X / Y / Z: 750 / 650 / 560 мм\nБрой инструменти: 60\nОбороти на шпиндела: 18 000 об/мин\nНаклон на ос А: ± 120°",
     "description": "Пет осен вертикален обработващ център с ЦПУ SIEMENS 840D за средно-габаритни призматично-корпусни детайли. Бърза смяна на инструмента (5 s) и висока скорост на позициониране намаляват производственото време."},
    {"section_title": "ФРЕЗОВИ ЦЕНТРОВЕ", "series_label": "5-ОСНО ФРЕЗОВАНЕ", "title": "DMG MORI Milltap 700",
     "image_filename": "dmg-mori-milltap700.jpg",
     "specs_text": "Ход X / Y / Z: 700 / 420 / 380 мм\nУправление: Siemens 840D SL\nОбороти на шпиндела: 20 - 10 000 об/мин\nМагазин с инструменти: 25 позиции\nСмяна на инструмент: 1,5 сек",
     "description": "Диаметър на масата 250 мм, пълна 5-осна обработка с висока скорост на подаване до 60 000 мм/мин."},
    {"section_title": "ФРЕЗОВИ ЦЕНТРОВЕ", "series_label": "3-ОСНО ФРЕЗОВАНЕ", "title": "HURCO BMC 30",
     "image_filename": "hurco-bmc30.jpg",
     "specs_text": "Маса / макс. товар: 1020 x 400 мм / 500 кг\nХод X / Y / Z: 760 / 460 / 600 мм\nУправление: Ultimax 3\nОбороти на шпиндела: 80 - 6 000 об/мин\nМагазин: 24, хоризонтален",
     "description": "Вертикален обработващ център с ЦПУ, 9 kW мощност, тегло 4,5 т."},
    {"section_title": "ФРЕЗОВИ ЦЕНТРОВЕ", "series_label": "3-ОСНО ФРЕЗОВАНЕ", "title": "HURCO BMC 4020 HT",
     "image_filename": "hurco-bmc4020ht.jpg",
     "specs_text": "Маса / макс. товар: 1220 x 510 мм / 682 кг\nХод X / Y / Z: 1016 / 510 / 610 мм\nУправление: Ultimax 4\nОбороти на шпиндела: 80 - 6 000 об/мин\nМагазин: 24 позиции",
     "description": "Мощност на шпиндела 11,2 / 14,9 kW, конус за инструменти SK 40."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "8-ОСЕН СТРУГ", "title": "GILDEMEISTER TWIN 42",
     "image_filename": "gildemeister-twin42.jpg",
     "specs_text": "Диаметър на струговане: 120 мм\nДължина на струговане: 650 мм\nОбороти на шпиндела: 35 - 7 000 об/мин\nЗадвижване на шпиндела: 25 kW",
     "description": "Двушпинделен струг с ЦПУ SIEMENS 840D, основен и контрашпиндел, 2x 12-позиционна кула, B-ос 180°, снабден с прътоподаващо устройство IEMCA BOSS 545 (до 3200 мм, Ф42)."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "8-ОСЕН СТРУГ", "title": "BENZINGER TNI-B8",
     "image_filename": "benzinger-tni-b8.jpg",
     "specs_text": "Брой шпиндели / револвери: 2 / 3\nОтвор на шпиндела: 32 мм\nОбороти на шпиндела: до 8 000 об/мин\nПозиции на револвери: 12 / 12 / 6",
     "description": "Мобилен заден шпиндел (12 kW), снабден с прътоподаващо устройство BREUNING IRCO за пръти до 3000 мм."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "6-ОСЕН СТРУГ", "title": "BENZINGER TNI-B6",
     "image_filename": "benzinger-tni-b6.jpg",
     "specs_text": "Брой шпиндели / револвери: 2 / 2\nОтвор на шпиндела: 32 мм\nОбороти на шпиндела: до 8 000 об/мин\nПозиции на купол 1 / 2: 12 / 12",
     "description": "Мобилен заден шпиндел (12 kW), прътоподаващо устройство BREUNING IRCO за пръти до 3000 мм."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "4-ОСЕН СТРУГ", "title": "DMG MORI CTX510 ecoline",
     "image_filename": "dmg-mori-ctx510.jpg",
     "specs_text": "Обработваем диаметър: 680 мм\nМаксимална дължина: 1050 мм\nОтвор на шпиндела: Ф76 мм\nОбороти на шпиндела: 3 250 об/мин",
     "description": "Стругов център с ЦПУ SIEMENS 840D - стругови и фрезови обработки при една установка на детайла, 12 инструмента."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "SWISS TYPE СТРУГ", "title": "STAR KNC 32",
     "image_filename": "star-knc32.jpg",
     "specs_text": "Макс. диаметър: 32 мм\nРеволверни глави: 2 x 6 живи инструмента\nЗадна обработка: 4 инструмента",
     "description": "Swiss type автоматичен струг със заден шпиндел за детайли с малък диаметър."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "SWISS TYPE СТРУГ", "title": "STAR KJR 16",
     "image_filename": "star-kjr16.jpg",
     "specs_text": "Макс. диаметър: 16 мм\nРеволверни глави: 2 x 6 живи инструмента\nЗадна обработка: 3 инструмента",
     "description": "Swiss type струг със заден шпиндел, снабден с прътоподаващо устройство FMB TURBO до 16 мм."},
    {"section_title": "СТРУГОВИ ЦЕНТРОВЕ", "series_label": "SWISS TYPE СТРУГ", "title": "STAR SVR-20",
     "image_filename": None,
     "specs_text": "Тип: Swiss type lathe",
     "description": "Автоматичен Swiss type струг за прецизна обработка на детайли с малък диаметър."},
    {"section_title": "ЛАЗЕРНО РЯЗАНЕ И ОГЪВАНЕ", "series_label": "FIBER LASER", "title": "FIBER LASER ECKERT",
     "image_filename": None,
     "specs_text": "Работна маса: 6000 x 2000 мм\nДебелина - стомана: 20 мм\nДебелина - неръждаема / алуминий: 10 мм\nМощност на лазера: 4 kW",
     "description": "Модел DIAMOND FIBER - рязане на листов материал и профили от черна, неръждаема стомана и алуминий, вкл. материал със защитно фолио, и гравиране с последващо рязане."},
    {"section_title": "ЛАЗЕРНО РЯЗАНЕ И ОГЪВАНЕ", "series_label": "FIBER LASER", "title": "CSF 3015/700",
     "image_filename": "csf-3015-laser.jpg",
     "specs_text": "Режеща площ: 3000 x 1500 мм\nТочност на позициониране: ± 0.05 мм\nМакс. скорост X/Y: 60 м/мин\nЛазерен източник: IPG",
     "description": "Стабилна конзолна стоманена конструкция, автоматична система за абсорбиране на прах (4 всмукателни отвора), ЦПУ управление O'LASERCUT."},
    {"section_title": "ЛАЗЕРНО РЯЗАНЕ И ОГЪВАНЕ", "series_label": "АБКАНТ ПРЕСА", "title": "DURMA AD-R 40175",
     "image_filename": "durma-ad-r-30135.jpg",
     "specs_text": "Усилие на сгъване: 175 тона\nДължина на сгъване: 4050 мм\nСветъл отвор: 530 мм\nГлавен двигател: 18.5 kW",
     "description": "CNC хидравлична абкантпреса за прецизно огъване на листов материал."},
    {"section_title": "ИЗМЕРВАНЕ И ДОВЪРШИТЕЛНА ОБРАБОТКА", "series_label": "ИЗМЕРВАТЕЛНА СИСТЕМА", "title": "DMG MORI UNO 20|40",
     "image_filename": "dmg-mori-uno2040.jpg",
     "specs_text": "Макс. диаметър на инструмент: 400 мм\nМакс. дължина: 400 мм\nЕкран: 19\", 45x увеличение",
     "description": "Прецизно измерване и настройка на режещи инструменти преди монтаж в машините."},
    {"section_title": "ИЗМЕРВАНЕ И ДОВЪРШИТЕЛНА ОБРАБОТКА", "series_label": "3D КООРДИНАТНО ИЗМЕРВАНЕ", "title": "Brown & Sharpe Derby 454 (CMM)",
     "image_filename": "etalon-derby-454-cmm.jpg",
     "specs_text": "Тип: 3D координатна измервателна машина",
     "description": "Пълен 3D контрол на качеството и точността на изработените детайли."},
    {"section_title": "ИЗМЕРВАНЕ И ДОВЪРШИТЕЛНА ОБРАБОТКА", "series_label": "ПОЛИРАНЕ / ДОВЪРШВАНЕ", "title": "Центрофужна дискова машина TE18 W",
     "image_filename": "te18w-polishing.jpg",
     "specs_text": "Обем на работната камера: 18 л\nМощност: 0,8 kW\nПартида: 3-4 кг",
     "description": "Довършителна повърхностна обработка - сваляне на заусенъци, заобляне на ръбове, обезмасляване, матиране и полиране на детайли."},
]


def seed_service_machine_cards():
    """Populates the services-page cards from SERVICE_MACHINE_CARDS_SEED, but only
    if none exist yet for that page - never re-adds a card someone deliberately
    deleted."""
    if ServiceMachineCard.query.filter_by(page='services').first():
        return
    for entry in SERVICE_MACHINE_CARDS_SEED:
        db.session.add(ServiceMachineCard(**entry))
    db.session.commit()


# One-time seed for the homepage's "МАШИНЕН ПАРК" highlight cards - was hardcoded
# HTML in index.html. Deliberately a separate, smaller curated set from the
# services-page cards above (different wording for some of the same machines),
# not shared rows - see the page column on ServiceMachineCard.
INDEX_MACHINE_CARDS_SEED = [
    {"page": "index", "series_label": "CNC MILLING // 5-ОСНО", "title": "DMG MORI DMU 75 monoBLOCK",
     "image_filename": "dmg-mori-dmu75.jpg",
     "specs_text": "Обороти на шпиндела: 18 000 об/мин\nРаботен ход (X/Y/Z): 750/650/560 мм\nИнструментален магазин: 60 инструмента",
     "description": "Пет осен вертикален обработващ център с ЦПУ SIEMENS 840D за средно-габаритни призматично-корпусни детайли с висока точност на позициониране."},
    {"page": "index", "series_label": "CNC TURNING // 8-ОСЕН", "title": "GILDEMEISTER TWIN 42",
     "image_filename": "gildemeister-twin42.jpg",
     "specs_text": "Диаметър на струговане: 120 мм\nДължина на струговане: 650 мм\nТип управление: SIEMENS 840D",
     "description": "Двушпинделен струг с основен и контрашпиндел, 2x 12-позиционна кула и прътоподаващо устройство IEMCA BOSS 545."},
    {"page": "index", "series_label": "SHEET PROCESSING // LASER", "title": "CSF 3015/700 лазерен център",
     "image_filename": "csf-3015-laser.jpg",
     "specs_text": "Режеща площ: 3000 x 1500 мм\nТочност на позициониране: ± 0.05 мм\nЛазерен източник: IPG",
     "description": "Автоматична система за абсорбиране на прах и стабилна конзолна стоманена конструкция за прецизно лазерно рязане."},
    {"page": "index", "series_label": "QUALITY CONTROL // CMM", "title": "Brown & Sharpe Derby 454",
     "image_filename": "etalon-derby-454-cmm.jpg",
     "specs_text": "Тип: 3D координатна измервателна машина\nПриложение: Контрол на качеството",
     "description": "3D измерване и контрол на качеството на всеки изработен детайл преди доставка."},
]


def seed_index_machine_cards():
    """Same idempotent pattern as seed_service_machine_cards(), scoped to page='index'."""
    if ServiceMachineCard.query.filter_by(page='index').first():
        return
    for entry in INDEX_MACHINE_CARDS_SEED:
        db.session.add(ServiceMachineCard(**entry))
    db.session.commit()


def process_entity(entity):
    """
    Reads a single DXF entity ONCE and extracts everything the app needs from
    it: its cutting length, its endpoint segments (for pierce/loop detection),
    and JSON-serializable shape(s) for the 2D viewer.

    Previously these three pieces of data were each computed via a separate
    full pass over every entity in the drawing (3x the iteration and 3x the
    ezdxf attribute-access overhead for large files). Combining them into one
    pass keeps behavior identical while roughly tripling geometry-extraction
    throughput on drawings with many entities.

    Returns a tuple: (length_contribution, segments, shapes)
    `shapes` is a list because a single polyline with bulges (rounded
    corners) decomposes into a mix of straight and arc sub-segments.
    """
    dtype = entity.dxftype()
    length = 0.0
    segments = []
    shapes = []

    try:
        if dtype == 'LINE':
            start = (entity.dxf.start.x, entity.dxf.start.y)
            end = (entity.dxf.end.x, entity.dxf.end.y)

            length = math.dist(start, end)
            segments.append((start, end))
            shapes.append({'type': 'line', 'x1': start[0], 'y1': start[1], 'x2': end[0], 'y2': end[1]})

        elif dtype == 'CIRCLE':
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            top_point = (cx, cy + r)

            length = 2 * math.pi * r
            # A circle is a closed loop that touches itself - model it as a
            # single segment starting and ending at the same point.
            segments.append((top_point, top_point))
            shapes.append({'type': 'circle', 'cx': cx, 'cy': cy, 'r': r})

        elif dtype == 'ARC':
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            start_angle, end_angle = entity.dxf.start_angle, entity.dxf.end_angle
            sa, ea = math.radians(start_angle), math.radians(end_angle)
            start = (cx + r * math.cos(sa), cy + r * math.sin(sa))
            end = (cx + r * math.cos(ea), cy + r * math.sin(ea))

            span = end_angle - start_angle
            if span < 0:
                span += 360
            length = r * math.radians(span)
            segments.append((start, end))
            shapes.append(
                {'type': 'arc', 'cx': cx, 'cy': cy, 'r': r, 'start_angle': start_angle, 'end_angle': end_angle})

        elif dtype in ('LWPOLYLINE', 'POLYLINE'):
            # Include bulge values (format='xyb'): a non-zero bulge means the
            # segment from this vertex to the next is actually a rounded arc,
            # not a straight line - skipping it (as the old code did) flattens
            # every rounded corner in the part into a sharp straight cut.
            # NOTE: ezdxf returns numpy.float64 for this format, not native
            # Python float. That silently poisons every downstream sum
            # (total_length, calculated_price) into numpy.float64, which
            # psycopg2 can't bind - causing an obscure "schema np does not
            # exist" error on INSERT. Cast to native float immediately.
            vertices = [(float(p[0]), float(p[1]), float(p[2])) for p in entity.get_points(format='xyb')]

            if vertices:
                segment_pairs = [(vertices[i], vertices[i + 1]) for i in range(len(vertices) - 1)]
                if entity.is_closed:
                    segment_pairs.append((vertices[-1], vertices[0]))

                for (x1, y1, bulge), (x2, y2, _next_bulge) in segment_pairs:
                    p1, p2 = (x1, y1), (x2, y2)
                    segments.append((p1, p2))
                    chord = math.dist(p1, p2)

                    is_straight = abs(bulge) < 1e-9
                    if not is_straight and chord > 0:
                        # A bulge's radius is derived by dividing by the
                        # bulge value, so tiny floating-point noise on what
                        # should be a straight segment (e.g. 1e-7 instead of
                        # exactly 0) produces a near-infinite radius and a
                        # center millions of mm away. That phantom arc is
                        # invisible on screen but blows out the bounding box
                        # used to scale/center the whole drawing. A radius
                        # more than 1000x the chord length is imperceptibly
                        # flat at any real drawing scale, so treat it as
                        # straight instead of trusting the raw bulge value.
                        center, start_rad, end_rad, radius = bulge_to_arc(p1, p2, bulge)
                        if not math.isfinite(radius) or radius > chord * 1000:
                            is_straight = True

                    if is_straight:
                        # Straight segment
                        length += chord
                        shapes.append({'type': 'line', 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2})
                    else:
                        # Curved segment - convert the bulge into real arc
                        # parameters (center, radius, start/end angle).
                        sweep_rad = (end_rad - start_rad) % (2 * math.pi)

                        length += radius * sweep_rad
                        shapes.append({
                            'type': 'arc',
                            'cx': center.x, 'cy': center.y, 'r': radius,
                            'start_angle': math.degrees(start_rad),
                            'end_angle': math.degrees(end_rad)
                        })

    except Exception:
        pass  # Ignore malformed entities safely, keep processing the rest

    return length, segments, shapes


def count_pierces(all_segments, tolerance=0.5):
    """
    Counts the number of separate closed loops/paths ("pierces") a laser/CNC
    head would need, by treating each entity's endpoints as graph nodes and
    grouping segments that touch (within `tolerance` mm) into connected
    components via BFS.

    Note: this is an O(n^2) comparison across all segment endpoints, which is
    fine for typical part drawings (hundreds of entities) but could get slow
    on DXF files with several thousand entities. If that ever becomes a
    bottleneck, a spatial grid/hash on endpoints would cut this down
    significantly.
    """
    num_segs = len(all_segments)
    if num_segs == 0:
        return 0

    adj = {i: [] for i in range(num_segs)}
    for i in range(num_segs):
        s1, e1 = all_segments[i]
        for j in range(i + 1, num_segs):
            s2, e2 = all_segments[j]
            if (math.dist(s1, s2) <= tolerance or
                    math.dist(s1, e2) <= tolerance or
                    math.dist(e1, s2) <= tolerance or
                    math.dist(e1, e2) <= tolerance):
                adj[i].append(j)
                adj[j].append(i)

    pierce_count = 0
    visited = set()
    for node in range(num_segs):
        if node not in visited:
            pierce_count += 1
            queue = [node]
            visited.add(node)
            while queue:
                curr = queue.pop(0)
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

    return pierce_count


def compute_bounding_box(shapes):
    """
    Computes the outer width/height of a drawing from its extracted shapes.
    This feeds directly into pricing, so arcs use their true angular sweep
    (not just their full-circle radius) to stay precise - a rounding-corner
    arc (say a 90-degree corner fillet) should only expand the box by its
    actual visible extent, not by treating it as if it were a full circle.
    """
    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')

    def expand(x, y):
        nonlocal min_x, max_x, min_y, max_y
        if x < min_x: min_x = x
        if x > max_x: max_x = x
        if y < min_y: min_y = y
        if y > max_y: max_y = y

    for s in shapes:
        if s['type'] == 'line':
            expand(s['x1'], s['y1'])
            expand(s['x2'], s['y2'])

        elif s['type'] == 'circle':
            expand(s['cx'] - s['r'], s['cy'] - s['r'])
            expand(s['cx'] + s['r'], s['cy'] + s['r'])

        elif s['type'] == 'arc':
            cx, cy, r = s['cx'], s['cy'], s['r']
            sa, ea = s['start_angle'] % 360, s['end_angle'] % 360
            sweep = (ea - sa) % 360 or 360  # 0 means a full 360-degree sweep

            # Always include the arc's actual start/end points.
            for angle in (sa, ea):
                rad = math.radians(angle)
                expand(cx + r * math.cos(rad), cy + r * math.sin(rad))

            # Include any cardinal direction (rightmost/top/leftmost/bottom
            # of the full circle) that the arc's sweep actually passes
            # through - those are the only points where the arc can extend
            # further than a straight line between its start/end would.
            for cardinal in (0, 90, 180, 270):
                if (cardinal - sa) % 360 <= sweep + 1e-9:
                    rad = math.radians(cardinal)
                    expand(cx + r * math.cos(rad), cy + r * math.sin(rad))

    if min_x == float('inf'):
        return 0.0, 0.0
    return max_x - min_x, max_y - min_y


def analyze_dxf_geometry(file_path):
    """
    Parses a DXF file to determine outer dimensions, total cutting length,
    a precise pierce count using direct entity extraction and graph matching,
    and a list of drawable shapes for the 2D viewer.
    """
    try:
        doc = ezdxf.readfile(file_path)
        msp = doc.modelspace()

        # 1. Single pass over every entity: accumulate cutting length, collect
        # endpoint segments (for pierce detection + bounding box), and collect
        # drawable shapes.
        total_length = 0.0
        all_segments = []
        shapes = []

        for entity in msp:
            entity_length, entity_segments, entity_shapes = process_entity(entity)
            total_length += entity_length
            all_segments.extend(entity_segments)
            shapes.extend(entity_shapes)

        # 2. Calculate outer dimensions from the SAME sanitized shape data
        # used for the 2D viewer and cutting length/pricing - not a separate
        # ezdxf bbox.extents() call over the raw entities. Deriving it
        # independently would let a degenerate entity (e.g. a near-zero
        # bulge producing a huge phantom arc, or a stray TEXT/DIMENSION
        # entity far from the actual part) silently inflate the *priced*
        # dimensions without showing up in what's actually drawn/cut, or
        # vice versa. Computing both from one sanitized source keeps price
        # and visualization guaranteed consistent.
        width, height = compute_bounding_box(shapes)
        if width == 0 and height == 0:
            # Fallback for files with no LINE/CIRCLE/ARC/POLYLINE geometry at
            # all (e.g. only SPLINE/HATCH/TEXT) - better to report ezdxf's
            # own bounding box than nothing.
            try:
                extents = bbox.extents(msp, fast=True)
                if extents.has_data:
                    width, height = extents.size.x, extents.size.y
            except Exception:
                pass

        # 3. Graph connectivity component counting to determine pierce count
        pierce_count = count_pierces(all_segments)

        # 4. Fallbacks to prevent returning zeros for weirdly scaled files
        if width == 0 and height == 0 and total_length > 0:
            width, height = 10.0, 10.0
        if pierce_count == 0 and total_length > 0:
            pierce_count = 1

        return float(abs(round(width, 2))), float(abs(round(height, 2))), float(
            abs(round(total_length, 2))), pierce_count, shapes

    except Exception as e:
        print(f"Critical DXF Parsing Error: {e}")
        return None, None, None, None, None


def calculate_cnc_price(width, height, total_length, pierce_count, material_key):
    material = MaterialPrice.query.filter_by(key=material_key).first()
    if not material:
        return 0.0

    # Prices are stored per square meter / per meter of cut (human-friendly),
    # so convert the drawing's mm-based measurements accordingly before
    # applying them. 1 m2 = 1,000,000 mm2; 1 m = 1,000 mm.
    area_m2 = (width * height) / 1_000_000
    length_m = total_length / 1_000

    material_surface_cost = area_m2 * material.cost_per_m2
    cutting_lineal_cost = length_m * material.cost_per_meter_cut
    piercing_total_cost = pierce_count * material.cost_per_pierce

    total_calculated_euro = material_surface_cost + cutting_lineal_cost + piercing_total_cost + BASE_SETUP_FEE
    return round(total_calculated_euro, 2)


def generate_order_number():
    """
    Generates a unique, human-friendly order number like ORD-2026-4821.
    Retries on the (very unlikely) chance of a random collision; falls back
    to a guaranteed-unique uuid-based suffix if it somehow never finds a free
    4-digit number.
    """
    year = datetime.utcnow().year
    for _ in range(20):
        candidate = f"ORD-{year}-{random.randint(1000, 9999)}"
        if not Order.query.filter_by(order_number=candidate).first():
            return candidate
    return f"ORD-{year}-{uuid.uuid4().hex[:8].upper()}"


def refresh_order_status(order):
    """
    Recomputes an order's status from its current production progress.
    Never overrides a cancelled order - cancellation is a manual, final
    action independent of production progress.
    """
    if order.status == 'cancelled':
        return
    pct = order.percent_complete
    if pct >= 100 and len(order.items) > 0:
        order.status = 'completed'
    elif pct > 0:
        order.status = 'in_production'
    else:
        order.status = 'new'


def order_missing_items(order):
    """
    Live shortfall check for one order: for each line, compares
    quantity_ordered against the current stock_quantity of the Detail/
    Product it points at (same finished-goods stock delivery notes bump -
    see CLAUDE.md order-fulfillment task) and returns only the lines that
    fall short. Recomputed on demand rather than snapshotted at order-
    creation time, so it stays correct as stock is replenished or other
    orders are placed - stock_quantity is never reserved/decremented
    anywhere in this app (see _bump_stock), so "available" here just means
    "on hand right now", same as everywhere else stock_quantity is read.
    Returns a list of {item_name, needed, available, missing} dicts.
    """
    shortfalls = []
    for item in order.items:
        if item.product_id:
            available = item.product.stock_quantity or 0
        elif item.detail_id:
            available = item.detail.stock_quantity or 0
        else:
            continue
        missing = item.quantity_ordered - available
        if missing > 0:
            shortfalls.append({
                'item_name': item.item_name,
                'needed': item.quantity_ordered,
                'available': available,
                'missing': missing,
            })
    return shortfalls


from functools import wraps


def role_required(roles):
    """Decorator to require specific roles."""

    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if isinstance(roles, str):
                allowed_roles = [roles]
            else:
                allowed_roles = roles

            if current_user.role not in allowed_roles:
                flash("Нямате разрешение за достъп до тази страница.", "danger")
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)

        return decorated_function

    return decorator


# ----------------- МАРШРУТИ И ЛОГИКА -----------------

@app.context_processor
def inject_current_year():
    return {'current_year': datetime.now().year}


def format_material_option(material):
    """
    Standardized display text for every material <select> in the app:
    "Name (Brand, Width mm, Length mm, Thickness mm)" - always all four
    slots, with a "-" placeholder for whichever of brand/width/length/
    thickness is blank/None, so every option in a dropdown lines up in the
    same shape regardless of how much data that particular row has.
    """
    parts = [material.brand or '-']
    for dim in (material.sheet_width_mm, material.sheet_length_mm, material.thickness_mm):
        parts.append(f"{dim:g}mm" if dim is not None else '-')
    return f"{material.display_name} ({', '.join(parts)})"


app.jinja_env.globals['get_text'] = get_text
app.jinja_env.globals['material_type_label'] = lambda key: MATERIAL_TYPE_LABELS.get(key, key)
app.jinja_env.globals['MATERIAL_TYPE_LABELS'] = MATERIAL_TYPE_LABELS
app.jinja_env.globals['format_material_option'] = format_material_option


@app.route('/robots.txt')
def robots_txt():
    # Public marketing pages (/, /services, /about, /contact) are the only
    # ones worth indexing - everything else requires login anyway. Just
    # allow everything rather than maintaining a path list here.
    return app.response_class('User-agent: *\nAllow: /\n', mimetype='text/plain')


@app.route('/')
def index():
    # Both anonymous and logged-in visitors see the public landing page now -
    # index.html adapts its nav CTA based on current_user.is_authenticated
    # (showing "Към Таблото" instead of Login/Register). Apps like /dashboard
    # and /generator still require login via @login_required regardless.
    machine_cards = ServiceMachineCard.query.filter_by(page='index').order_by(ServiceMachineCard.id).all()
    return render_template('index.html', active_page='index', machine_cards=machine_cards)


def _group_service_cards_by_section(cards):
    """
    Groups cards into their section headers (e.g. "ФРЕЗОВИ ЦЕНТРОВЕ"), keyed
    by section_title regardless of insertion order - a new machine added to
    an existing section must join that section's card grid, not spawn a
    second same-titled section further down the page just because a
    differently-sectioned card was created in between (cards are ordered by
    id, i.e. creation time, so sections interleave over time). Cards with no
    section_title (added via the "+ Добави машина" popup) fall into one
    trailing "ДОПЪЛНИТЕЛНИ МАШИНИ" bucket, so every future addition lands
    together. First-appearance order of each title is preserved. Pure/no DB
    calls itself, so it's testable without a live database - see
    test_service_sections_grouping.py.
    """
    sections_by_title = {}
    sections = []
    for card in cards:
        title = card.section_title or 'ДОПЪЛНИТЕЛНИ МАШИНИ'
        if title not in sections_by_title:
            sections_by_title[title] = {'title': title, 'cards': []}
            sections.append(sections_by_title[title])
        sections_by_title[title]['cards'].append(card)
    return sections


@app.route('/services')
def services():
    cards = ServiceMachineCard.query.filter_by(page='services').order_by(ServiceMachineCard.id).all()
    sections = _group_service_cards_by_section(cards)
    return render_template('services.html', active_page='services', machine_sections=sections)


@app.route('/about')
def about():
    return render_template('about.html', active_page='about')


@app.route('/contact')
def contact():
    return render_template('contact.html', active_page='contact')


@app.route('/generator')
@login_required
def generator():
    # Requires login, same as every other app (matches the "apps require an
    # account, the public site doesn't" design used across the project).
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    return render_template('generator.html', materials=materials, active_page='generator')


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
        flash('Невалидно потребителско име или парола.')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password', '')

        # Same 8-character floor migration/change_admin_password.py already enforces
        # for the admin account - short passwords are well within the
        # per-minute brute-force budget /login's rate limit still allows.
        if len(password) < 8:
            flash('Паролата трябва да бъде поне 8 символа.', 'danger')
            return redirect(url_for('register'))

        # Hash the password
        hashed_password = generate_password_hash(password, method='scrypt')

        # Create user with default role 'regular_user'
        new_user = User(
            username=username,
            password=hashed_password,
            role='regular_user'  # Make sure this is 'role', not 'is_admin'
        )

        db.session.add(new_user)
        db.session.commit()

        flash('Регистрацията е успешна!', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


# Characters with no legitimate reason to appear in a display filename but
# that matter to an HTML/JS parser (quotes, angle brackets, control chars,
# '&'). Unlike werkzeug's secure_filename(), this keeps non-ASCII names
# (e.g. Cyrillic) intact - it's for safe *display* in templates, not for
# building a filesystem path.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>"\'&]')


def sanitize_display_filename(filename):
    """
    Strips characters that could enable HTML/JS injection if this filename
    is later rendered in a template, while preserving the user-visible name
    otherwise. Defense in depth: templates must still escape this value
    correctly for whatever context they place it in (see dashboard.html's
    data-filename attribute), but a stored value that's already free of
    quotes/angle-brackets/control-chars can't be used to break out of any
    context in the first place.
    """
    return _UNSAFE_FILENAME_CHARS.sub('', filename)


# Окончателно възстановен маршут за потребителското табло
def process_dxf_upload(file, material_key, machine_id=None):
    """
    Shared DXF-upload pipeline used by both /dashboard and /upload: saves
    the file to a temp path, extracts geometry, validates the material, and
    builds a (not-yet-committed) DxfFile record with its calculated price.
    Keeping this logic in one place means a future fix to it automatically
    applies to both routes, instead of having to be made twice.

    Returns a (dxf_file, pierce_count, error_message) tuple - on failure
    dxf_file/pierce_count are None and error_message is a user-facing
    Bulgarian message ready to flash(); on success error_message is None.
    """
    temp_path = None
    try:
        filename = secure_filename(file.filename)
        # Save to the private upload folder (not the public static/ folder)
        # with a unique prefix, so concurrent uploads never collide and the
        # raw file is never briefly web-accessible.
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
        file.save(temp_path)

        # Extracts geometric metrics, pierce count, and drawable shapes
        width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(temp_path)
        if width is None or total_length is None:
            return None, None, 'Грешка при обработката на DXF структурата.'

        material_row = MaterialPrice.query.filter_by(key=material_key).first()
        if not material_row:
            return None, None, 'Невалиден избор на материал.'

        price = calculate_cnc_price(width, height, total_length, pierce_count, material_key)

        dxf_file = DxfFile(
            filename=sanitize_display_filename(file.filename),
            material=material_key,
            width=width,
            height=height,
            total_length=total_length,
            calculated_price=price,
            user_id=current_user.id,
            geometry_json=json.dumps(shapes),
            machine_id=machine_id
        )
        return dxf_file, pierce_count, None
    finally:
        # Always clean up the temp file, regardless of success/failure.
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@app.route('/dashboard')
@login_required
def dashboard():
    # Pure library view now - uploading/calculating a new DXF lives on its
    # own page (see upload()) so the two don't get conflated in the nav.
    user_uploads = DxfFile.query.filter_by(user_id=current_user.id).order_by(DxfFile.id.desc()).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    return render_template('dashboard.html', uploads=user_uploads, materials=materials, active_page='dashboard')


@app.route('/geometry/<int:file_id>')
@login_required
def get_geometry(file_id):
    """
    Returns the stored 2D shape data for a given uploaded DXF file, so the
    dashboard viewer modal can render it on a canvas. Only the owning user
    (or an admin) may access it.
    """
    dxf_file = DxfFile.query.get_or_404(file_id)

    if dxf_file.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Нямате достъп до този файл.'}), 403

    try:
        shapes = json.loads(dxf_file.geometry_json) if dxf_file.geometry_json else []
    except (TypeError, ValueError):
        shapes = []

    return jsonify({
        'filename': dxf_file.filename,
        'width': dxf_file.width,
        'height': dxf_file.height,
        'shapes': shapes
    })


@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    # Потребителят трие сам своя профил
    user = User.query.get(current_user.id)
    logout_user()
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('register'))


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or file.filename == '':
            flash("Моля, изберете файл за качване.", "danger")
            return redirect(request.url)

        if not file.filename.lower().endswith('.dxf'):
            flash('Невалиден формат! Системата приема само .dxf файлове.', 'danger')
            return redirect(request.url)

        try:
            chosen_material = request.form.get('material', 'steel')
            machine_id_raw = request.form.get('machine_id', '')
            selected_machine = int(machine_id_raw) if machine_id_raw and machine_id_raw.isdigit() else None

            dxf_file, _pierce_count, error = process_dxf_upload(file, chosen_material, machine_id=selected_machine)
            if error:
                flash(error, 'danger')
                return redirect(request.url)

            db.session.add(dxf_file)
            db.session.commit()
            flash(f'Файлът "{file.filename}" беше качен и обработен успешно!', 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.rollback()
            flash(f'Критична грешка при обработка/запис: {str(e)}', 'danger')
            return redirect(request.url)

    machines = Machine.query.all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    return render_template('upload.html', machines=machines, materials=materials, active_page='upload')

# ----------------- АДМИНИСТРАТОРСКИ МАРШРУТИ -----------------

@app.route('/admin')
@login_required
def admin_dashboard():
    """
    Hub page: just counts + links into the dedicated sub-pages below (users/
    materials/details/products/clients) plus the cross-cutting ERP № lookup
    box - each domain's own CRUD lives on its own route/template now instead
    of one long admin.html.
    """
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    counts = {
        'users': User.query.count(),
        'materials': MaterialPrice.query.count(),
        'details': Detail.query.count(),
        'products': Product.query.count(),
        'clients': Client.query.count(),
        'deliverers': Deliverer.query.count(),
        'suppliers': Supplier.query.count(),
    }
    return render_template('admin.html', counts=counts, active_page='admin')


@app.route('/admin/users')
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    all_users = User.query.filter(User.id != current_user.id).all()
    return render_template('admin_users.html', users=all_users, active_page='admin_users')


@app.route('/admin/materials')
@login_required
def admin_materials():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    return render_template('admin_materials.html', materials=materials, active_page='admin_materials')


@app.route('/admin/details')
@login_required
def admin_details():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    details = Detail.query.order_by(Detail.name).all()
    return render_template('admin_details.html', materials=materials, details=details, active_page='admin_details')


@app.route('/admin/products')
@login_required
def admin_products():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    products = Product.query.order_by(Product.name).all()
    product_pricing = {p.id: calculate_product_pricing(p) for p in products}
    return render_template('admin_products.html', products=products, product_pricing=product_pricing, active_page='admin_products')


@app.route('/admin/clients')
@login_required
def admin_clients():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    clients = Client.query.order_by(Client.name).all()
    deliverers = Deliverer.query.order_by(Deliverer.name).all()
    return render_template('admin_clients.html', clients=clients, deliverers=deliverers, active_page='admin_clients')


@app.route('/admin/clients/add', methods=['POST'])
@login_required
def admin_add_client():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на клиента.', 'danger')
        return redirect(url_for('admin_clients'))
    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        flash(eik_error, 'danger')
        return redirect(url_for('admin_clients'))
    client_type = 'company' if request.form.get('client_type') == 'company' else 'individual'
    client = Client(
        name=name,
        email=request.form.get('email', '').strip() or None,
        phone=request.form.get('phone', '').strip() or None,
        client_type=client_type,
        eik=eik,
        vat_number=request.form.get('vat_number', '').strip() or None,
        address=request.form.get('address', '').strip() or None,
        mol=request.form.get('mol', '').strip() or None,
    )
    db.session.add(client)
    db.session.commit()
    flash(f'Клиентът "{name}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_clients'))


@app.route('/admin/clients/<int:client_id>/edit')
@login_required
def edit_client_window(client_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/clients."""
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    client = Client.query.get_or_404(client_id)
    return render_template(
        'edit_window.html', item_label='клиент', saved=request.args.get('saved') == '1',
        action=url_for('update_client', client_id=client.id),
        fields=[
            {'name': 'name', 'label': 'Име', 'value': client.name, 'type': 'text', 'required': True},
            {'name': 'client_type', 'label': 'Тип клиент', 'value': client.client_type, 'type': 'select',
             'options': [{'value': 'individual', 'label': 'Физическо лице'}, {'value': 'company', 'label': 'Юридическо лице'}]},
            {'name': 'email', 'label': 'Имейл', 'value': client.email or '', 'type': 'text'},
            {'name': 'phone', 'label': 'Телефон', 'value': client.phone or '', 'type': 'text'},
            {'name': 'eik', 'label': 'ЕИК / Булстат', 'value': client.eik or '', 'type': 'text',
             'pattern': r'\d{9}', 'maxlength': 9, 'inputmode': 'numeric', 'title': 'Точно 9 цифри'},
            {'name': 'vat_number', 'label': 'ИН по ДДС', 'value': client.vat_number or '', 'type': 'text'},
            {'name': 'address', 'label': 'Адрес на управление', 'value': client.address or '', 'type': 'text'},
            {'name': 'mol', 'label': 'МОЛ', 'value': client.mol or '', 'type': 'text'},
        ]
    )


@app.route('/admin/clients/<int:client_id>/update', methods=['POST'])
@login_required
def update_client(client_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    client = Client.query.get_or_404(client_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на клиента.', 'danger')
        return redirect(url_for('admin_clients'))
    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        flash(eik_error, 'danger')
        return redirect(url_for('admin_clients'))
    client.name = name
    client.client_type = 'company' if request.form.get('client_type') == 'company' else 'individual'
    client.email = request.form.get('email', '').strip() or None
    client.phone = request.form.get('phone', '').strip() or None
    client.eik = eik
    client.vat_number = request.form.get('vat_number', '').strip() or None
    client.address = request.form.get('address', '').strip() or None
    client.mol = request.form.get('mol', '').strip() or None
    db.session.commit()
    flash('Клиентът беше обновен успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_client_window', client_id=client_id, saved='1'))
    return redirect(url_for('admin_clients'))


@app.route('/admin/clients/<int:client_id>/delete', methods=['POST'])
@login_required
def admin_delete_client(client_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    client = Client.query.get_or_404(client_id)
    # Orders referencing this client keep existing (client_id is nullable) -
    # detach rather than block deletion, same pattern as delete_machine().
    Order.query.filter_by(client_id=client.id).update({'client_id': None})
    db.session.delete(client)
    db.session.commit()
    flash(f'Клиентът "{client.name}" беше изтрит.', 'success')
    return redirect(url_for('admin_clients'))


@app.route('/admin/deliverers/add', methods=['POST'])
@login_required
def admin_add_deliverer():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на куриера.', 'danger')
        return redirect(url_for('admin_clients'))
    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        flash(eik_error, 'danger')
        return redirect(url_for('admin_clients'))
    deliverer = Deliverer(
        name=name,
        email=request.form.get('email', '').strip() or None,
        phone=request.form.get('phone', '').strip() or None,
        eik=eik,
        vat_number=request.form.get('vat_number', '').strip() or None,
        address=request.form.get('address', '').strip() or None,
        mol=request.form.get('mol', '').strip() or None,
    )
    db.session.add(deliverer)
    db.session.commit()
    flash(f'Куриерът "{name}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_clients'))


@app.route('/admin/deliverers/<int:deliverer_id>/edit')
@login_required
def edit_deliverer_window(deliverer_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/clients, mirrors edit_client_window."""
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    deliverer = Deliverer.query.get_or_404(deliverer_id)
    return render_template(
        'edit_window.html', item_label='куриер', saved=request.args.get('saved') == '1',
        action=url_for('update_deliverer', deliverer_id=deliverer.id),
        fields=[
            {'name': 'name', 'label': 'Име', 'value': deliverer.name, 'type': 'text', 'required': True},
            {'name': 'email', 'label': 'Имейл', 'value': deliverer.email or '', 'type': 'text'},
            {'name': 'phone', 'label': 'Телефон', 'value': deliverer.phone or '', 'type': 'text'},
            {'name': 'eik', 'label': 'ЕИК / Булстат', 'value': deliverer.eik or '', 'type': 'text',
             'pattern': r'\d{9}', 'maxlength': 9, 'inputmode': 'numeric', 'title': 'Точно 9 цифри'},
            {'name': 'vat_number', 'label': 'ИН по ДДС', 'value': deliverer.vat_number or '', 'type': 'text'},
            {'name': 'address', 'label': 'Адрес на управление', 'value': deliverer.address or '', 'type': 'text'},
            {'name': 'mol', 'label': 'МОЛ', 'value': deliverer.mol or '', 'type': 'text'},
        ]
    )


@app.route('/admin/deliverers/<int:deliverer_id>/update', methods=['POST'])
@login_required
def update_deliverer(deliverer_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    deliverer = Deliverer.query.get_or_404(deliverer_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на куриера.', 'danger')
        return redirect(url_for('admin_clients'))
    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        flash(eik_error, 'danger')
        return redirect(url_for('admin_clients'))
    deliverer.name = name
    deliverer.email = request.form.get('email', '').strip() or None
    deliverer.phone = request.form.get('phone', '').strip() or None
    deliverer.eik = eik
    deliverer.vat_number = request.form.get('vat_number', '').strip() or None
    deliverer.address = request.form.get('address', '').strip() or None
    deliverer.mol = request.form.get('mol', '').strip() or None
    db.session.commit()
    flash('Куриерът беше обновен успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_deliverer_window', deliverer_id=deliverer_id, saved='1'))
    return redirect(url_for('admin_clients'))


@app.route('/admin/deliverers/<int:deliverer_id>/delete', methods=['POST'])
@login_required
def admin_delete_deliverer(deliverer_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    deliverer = Deliverer.query.get_or_404(deliverer_id)
    Order.query.filter_by(deliverer_id=deliverer.id).update({'deliverer_id': None})
    db.session.delete(deliverer)
    db.session.commit()
    flash(f'Куриерът "{deliverer.name}" беше изтрит.', 'success')
    return redirect(url_for('admin_clients'))


def _bump_stock(target, quantity):
    """Adds `quantity` to target.stock_quantity and returns the new total.
    Pure/no DB calls itself, so it's testable without a live database - see
    test_delivery_note_stock.py."""
    target.stock_quantity = (target.stock_quantity or 0) + quantity
    return target.stock_quantity


DELIVERY_NOTE_TARGET_MODELS = {'material': MaterialPrice, 'detail': Detail, 'product': Product}


def _find_or_create_delivery_target(item_type, name, brand, width, height, thickness, unit_price, material_key,
                                     cost_per_m2=None, cost_per_meter_cut=None, cost_per_pierce=None, components=None):
    """
    Resolves one delivery-note line to a catalog row: reuses an existing
    Material/Detail/Product only if every descriptive field matches exactly,
    otherwise creates a brand-new bare-bones row (no DXF geometry / BOM -
    just what the paper delivery note itself carries). This is deliberate -
    two lines that differ in even one parameter (brand, a dimension, or
    price) must never be folded into the same stock count, per the
    "keep separate items separate" rule (see CLAUDE.md delivery-note task).

    Detail still requires material_key (hard FK, see admin_delete_material's
    docstring) - a bare-bones Detail can skip total_length/pierce_count
    (no DXF was uploaded), but not the material it's cut from.
    Product carries no dimension/brand/price columns of its own, so a
    brand-new Product only matches/dedupes on name; `components` (an
    optional {detail_id: quantity} dict, already validated by the caller)
    is only applied when a brand-new Product row is created - unlike
    material/detail it's not required, since a bare product (no BOM yet) is
    a normal, pre-existing state here (same as api_quick_create_product with
    no components attached).

    A *new* material must come with real cost_per_m2/cost_per_meter_cut/
    cost_per_pierce (mirrors admin_add_material's required fields) - without
    them we'd otherwise silently create a zero-priced row that produces
    €0.00 CNC prices everywhere it's later picked. Returns None (skip the
    line) rather than defaulting to 0.0. Matching an *existing* material is
    unaffected - those keep whatever pricing they already have.
    """
    name = (name or '').strip()
    brand = (brand or '').strip() or None

    if item_type == 'material':
        existing = MaterialPrice.query.filter_by(
            display_name=name, brand=brand, sheet_width_mm=width,
            sheet_length_mm=height, thickness_mm=thickness
        ).first()
        if existing:
            return existing
        if cost_per_m2 is None or cost_per_meter_cut is None or cost_per_pierce is None:
            return None
        new_row = MaterialPrice(
            key='pending', display_name=name, cost_per_m2=cost_per_m2, cost_per_meter_cut=cost_per_meter_cut,
            cost_per_pierce=cost_per_pierce, sheet_width_mm=width, sheet_length_mm=height,
            thickness_mm=thickness, brand=brand, type='sheets', erp_number=_next_erp_number(),
        )
        db.session.add(new_row)
        db.session.flush()
        new_row.key = f'material_{new_row.id}'
        return new_row

    if item_type == 'detail':
        if not material_key or not MaterialPrice.query.filter_by(key=material_key).first():
            return None
        existing = Detail.query.filter_by(
            name=name, material_key=material_key, width=width or 0.0,
            height=height or 0.0, calculated_price=unit_price or 0.0
        ).first()
        if existing:
            return existing
        if unit_price is None:
            return None
        new_row = Detail(
            name=name, material_key=material_key, width=width or 0.0, height=height or 0.0,
            total_length=0.0, pierce_count=0, calculated_price=unit_price,
            erp_number=_next_erp_number(),
        )
        db.session.add(new_row)
        db.session.flush()
        return new_row

    if item_type == 'product':
        existing = Product.query.filter_by(name=name).first()
        if existing:
            return existing
        new_row = Product(name=name, erp_number=_next_erp_number())
        db.session.add(new_row)
        db.session.flush()
        for detail_id, quantity in (components or {}).items():
            db.session.add(ProductDetail(product_id=new_row.id, detail_id=detail_id, quantity=quantity))
        return new_row

    return None


@app.route('/admin/delivery-notes')
@role_required(['admin', 'worker'])
def admin_delivery_notes():
    """
    Intake page for restocking Materials/Details/Products from a supplier's
    delivery note (see CLAUDE.md task: manual form mimicking the paper
    layout, no OCR). Building one line item at a time mirrors order_create.
    html's cart pattern - pick a type, pick the specific catalog row, add a
    row - just with material/detail/product instead of product/detail.
    """
    suppliers = Supplier.query.order_by(Supplier.name).all()
    notes = DeliveryNote.query.order_by(DeliveryNote.created_at.desc()).all()
    # Ordered by type first so the template's |groupby('type') optgroups
    # (same structural-type sectioning as partials/material_options.html)
    # come out contiguous - Jinja's groupby needs pre-sorted input.
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    details = Detail.query.order_by(Detail.name).all()
    products = Product.query.order_by(Product.name).all()
    # Plain dicts (not ORM rows) for the client-side |tojson item picker -
    # same convention as create_order()'s products_data/details_data. width/
    # height/thickness/brand pre-fill the (editable) line-item fields on the
    # delivery note form from each catalog row's own parameters.
    materials_data = [{'name': m.display_name, 'width': m.sheet_width_mm, 'height': m.sheet_length_mm,
                        'thickness': m.thickness_mm, 'brand': m.brand, 'price': None} for m in materials]
    details_data = [{'name': d.name, 'width': d.width, 'height': d.height,
                      'thickness': d.material.thickness_mm if d.material else None,
                      'brand': d.material.brand if d.material else None,
                      'material_key': d.material_key, 'price': d.calculated_price} for d in details]
    products_data = [{'name': p.name, 'width': None, 'height': None, 'thickness': None, 'brand': None, 'price': None} for p in products]
    return render_template(
        'admin_delivery_notes.html', suppliers=suppliers, notes=notes,
        materials=materials, details=details, products=products,
        materials_data=materials_data, details_data=details_data, products_data=products_data,
        active_page='admin_delivery_notes'
    )


@app.route('/admin/suppliers/add', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_supplier():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на доставчика.', 'danger')
        return redirect(url_for('admin_delivery_notes'))
    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        flash(eik_error, 'danger')
        return redirect(url_for('admin_delivery_notes'))
    supplier = Supplier(
        name=name,
        eik=eik,
        vat_number=request.form.get('vat_number', '').strip() or None,
        phone=request.form.get('phone', '').strip() or None,
        email=request.form.get('email', '').strip() or None,
    )
    db.session.add(supplier)
    db.session.commit()
    flash(f'Доставчикът "{name}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_delivery_notes'))


@app.route('/admin/delivery-notes/create', methods=['POST'])
@role_required(['admin', 'worker'])
def create_delivery_note():
    """
    Records a delivery note and bumps stock_quantity on every referenced
    Material/Detail/Product - creating a new bare-bones catalog row when a
    line doesn't exactly match an existing one (see
    _find_or_create_delivery_target). items_json follows
    [{type, name, material_key, qty, unit_price, width, height, thickness,
    brand, notes, cost_per_m2, cost_per_meter_cut, cost_per_pierce,
    components}] - no id, since the row to bump/create is resolved from
    the line's own fields, not a pre-picked id (that's what used to let a
    line silently bump the wrong/dissimilar catalog row). `components`
    ([{detail_id, quantity}]) only applies to a brand-new product line -
    see _find_or_create_delivery_target.
    """
    try:
        items = json.loads(request.form.get('items_json', ''))
        if not isinstance(items, list):
            items = []
    except (TypeError, ValueError):
        items = []

    if not items:
        flash('Моля добавете поне един артикул към стоковата разписка.', 'danger')
        return redirect(url_for('admin_delivery_notes'))

    supplier_id_raw = request.form.get('supplier_id', '')
    supplier_id = int(supplier_id_raw) if supplier_id_raw and supplier_id_raw.isdigit() else None
    note_date_raw = request.form.get('note_date', '').strip()
    note_date = datetime.strptime(note_date_raw, '%Y-%m-%d').date() if note_date_raw else None

    note = DeliveryNote(
        supplier_id=supplier_id,
        note_number=request.form.get('note_number', '').strip() or None,
        note_date=note_date,
        created_by_id=current_user.id,
    )
    db.session.add(note)
    db.session.flush()

    added_any = False
    for row in items:
        if not isinstance(row, dict):
            continue
        item_type = row.get('type')
        if item_type not in DELIVERY_NOTE_TARGET_MODELS:
            continue
        name = (row.get('name') or '').strip()
        if not name:
            continue
        try:
            quantity = float(row.get('qty', 0))
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue

        def _optional_float(key):
            raw = row.get(key)
            try:
                return float(raw) if raw not in (None, '') else None
            except (TypeError, ValueError):
                return None

        unit_price = _optional_float('unit_price')
        width = _optional_float('width')
        height = _optional_float('height')
        thickness = _optional_float('thickness')
        brand = (row.get('brand') or '').strip() or None
        material_key = (row.get('material_key') or '').strip() or None
        cost_per_m2 = _optional_float('cost_per_m2')
        cost_per_meter_cut = _optional_float('cost_per_meter_cut')
        cost_per_pierce = _optional_float('cost_per_pierce')

        components = {}  # detail_id -> quantity, merging duplicates; malformed/unknown entries are just skipped
        for comp in (row.get('components') or []):
            if not isinstance(comp, dict):
                continue
            try:
                comp_detail_id = int(comp.get('detail_id'))
                comp_quantity = int(comp.get('quantity'))
            except (TypeError, ValueError):
                continue
            if comp_quantity < 1 or not Detail.query.get(comp_detail_id):
                continue
            components[comp_detail_id] = components.get(comp_detail_id, 0) + comp_quantity

        target = _find_or_create_delivery_target(
            item_type, name, brand, width, height, thickness, unit_price, material_key,
            cost_per_m2=cost_per_m2, cost_per_meter_cut=cost_per_meter_cut, cost_per_pierce=cost_per_pierce,
            components=components
        )
        if not target:
            continue

        description = target.display_name if item_type == 'material' else target.name
        db.session.add(DeliveryNoteItem(
            delivery_note_id=note.id, target_type=item_type, target_id=target.id,
            description_snapshot=description, quantity=quantity, unit_price=unit_price,
            notes=(row.get('notes') or '').strip() or None,
            width=width, height=height, thickness=thickness, brand=brand,
        ))
        _bump_stock(target, quantity)
        added_any = True

    if not added_any:
        db.session.rollback()
        flash('Няма валидни артикули за добавяне.', 'danger')
        return redirect(url_for('admin_delivery_notes'))

    db.session.commit()
    flash('Стоковата разписка беше записана и наличностите бяха обновени.', 'success')
    return redirect(url_for('admin_delivery_notes'))


@app.route('/admin/content')
@login_required
def admin_content():
    """
    Scoped-down content editor for the 'web_designer' role (and admins):
    only info text - detail names, product name/description - none of the
    pricing/catalog-management surface that lives on admin_dashboard.
    """
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    details = Detail.query.order_by(Detail.name).all()
    products = Product.query.order_by(Product.name).all()
    return render_template('content_editor.html', details=details, products=products, active_page='content')


def _machine_card_home(page):
    """Which public route a card's 'back to the page' redirect goes to."""
    return url_for('index') if page == 'index' else url_for('services')


def _service_section_titles():
    """Distinct section_title values already used on the services page (e.g.
    'ФРЕЗОВИ ЦЕНТРОВЕ'), for the add/edit machine popup's section datalist."""
    rows = db.session.query(ServiceMachineCard.section_title).filter(
        ServiceMachineCard.page == 'services', ServiceMachineCard.section_title.isnot(None)
    ).distinct().order_by(ServiceMachineCard.section_title).all()
    return [r[0] for r in rows]


MACHINE_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
# Only filenames our own upload code produces look like this - the seeded
# cards' images (e.g. "dmg-mori-dmu75.jpg") never do, and several seeded
# cards on different pages deliberately share the same committed image file,
# so only a file matching this prefix is ever safe to delete from disk.
_UPLOADED_IMAGE_PREFIX_RE = re.compile(r'^[0-9a-f]{32}_')


def _save_machine_card_image(file):
    """Saves an uploaded machine-card image into MACHINE_IMAGES_FOLDER with a
    collision-safe filename; returns the saved filename, or None if no valid
    image file was submitted."""
    if not file or file.filename == '':
        return None
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in MACHINE_IMAGE_EXTENSIONS:
        return None
    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4().hex}_{filename}"
    file.save(os.path.join(app.config['MACHINE_IMAGES_FOLDER'], unique_filename))
    return unique_filename


@app.route('/services/machine-cards/new')
@login_required
def new_machine_card_window():
    """Popup 'add new machine card' window - opened from services.html or index.html."""
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    page = 'index' if request.args.get('page') == 'index' else 'services'
    fields = [
        {'name': 'series_label', 'label': 'Кратък етикет (напр. 5-ОСНО ФРЕЗОВАНЕ)', 'value': '', 'type': 'text'},
        {'name': 'title', 'label': 'Име на машината', 'value': '', 'type': 'text', 'required': True},
        {'name': 'image', 'label': 'Снимка на машината (по избор)', 'value': '', 'type': 'file'},
    ]
    if page == 'services':
        # Only the services page groups cards by section - pick an existing
        # section (e.g. "ФРЕЗОВИ ЦЕНТРОВЕ") or type a brand-new one.
        fields.append({'name': 'section_title', 'label': 'Раздел (напр. фрезоване, струговане, листообработка)',
                        'value': '', 'type': 'datalist', 'options': _service_section_titles()})
    fields += [
        {'name': 'specs_text', 'label': 'Характеристики (незадължително, по един ред "Етикет: Стойност")', 'value': '', 'type': 'textarea'},
        {'name': 'description', 'label': 'Описание', 'value': '', 'type': 'textarea'},
    ]
    return render_template(
        'edit_window.html', item_label='нова машина', saved=request.args.get('saved') == '1',
        action=url_for('create_machine_card', page=page), fields=fields
    )


@app.route('/services/machine-cards/create', methods=['POST'])
@login_required
def create_machine_card():
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    page = 'index' if request.args.get('page') == 'index' else 'services'
    redirect_target = _machine_card_home(page)

    title = request.form.get('title', '').strip()
    if not title:
        flash('Моля въведете име на машината.', 'danger')
        return redirect(redirect_target)

    card = ServiceMachineCard(
        page=page,
        title=title,
        series_label=request.form.get('series_label', '').strip() or None,
        section_title=request.form.get('section_title', '').strip() or None if page == 'services' else None,
        specs_text=request.form.get('specs_text', '').strip() or None,
        description=request.form.get('description', '').strip() or None,
        image_filename=_save_machine_card_image(request.files.get('image')),
    )
    db.session.add(card)
    db.session.commit()
    flash('Машината беше добавена успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('new_machine_card_window', page=page, saved='1'))
    return redirect(redirect_target)


@app.route('/services/machine-cards/<int:card_id>/edit')
@login_required
def edit_machine_card_window(card_id):
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    card = ServiceMachineCard.query.get_or_404(card_id)
    fields = [
        {'name': 'series_label', 'label': 'Кратък етикет (напр. 5-ОСНО ФРЕЗОВАНЕ)', 'value': card.series_label or '', 'type': 'text'},
        {'name': 'title', 'label': 'Име на машината', 'value': card.title, 'type': 'text', 'required': True},
        {'name': 'image', 'label': 'Снимка на машината (по избор - оставете празно, за да запазите текущата)', 'value': '', 'type': 'file',
         'preview_url': url_for('static', filename='img/machines/' + card.image_filename) if card.image_filename else None},
    ]
    if card.page == 'services':
        fields.append({'name': 'section_title', 'label': 'Раздел (напр. фрезоване, струговане, листообработка)',
                        'value': card.section_title or '', 'type': 'datalist', 'options': _service_section_titles()})
    fields += [
        {'name': 'specs_text', 'label': 'Характеристики (незадължително, по един ред "Етикет: Стойност")', 'value': card.specs_text or '', 'type': 'textarea'},
        {'name': 'description', 'label': 'Описание', 'value': card.description or '', 'type': 'textarea'},
    ]
    return render_template(
        'edit_window.html', item_label='машина', saved=request.args.get('saved') == '1',
        action=url_for('update_machine_card', card_id=card.id), fields=fields
    )


@app.route('/services/machine-cards/<int:card_id>/update', methods=['POST'])
@login_required
def update_machine_card(card_id):
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    card = ServiceMachineCard.query.get_or_404(card_id)
    redirect_target = _machine_card_home(card.page)
    title = request.form.get('title', '').strip()
    if not title:
        flash('Моля въведете име на машината.', 'danger')
        return redirect(redirect_target)

    card.title = title
    card.series_label = request.form.get('series_label', '').strip() or None
    if card.page == 'services':
        card.section_title = request.form.get('section_title', '').strip() or None
    card.specs_text = request.form.get('specs_text', '').strip() or None
    card.description = request.form.get('description', '').strip() or None

    new_image_filename = _save_machine_card_image(request.files.get('image'))
    if new_image_filename:
        # Only ever delete files our own upload code produced - several
        # seeded cards deliberately share the same committed image file, so
        # a seed filename must never be removed from disk here.
        if card.image_filename and _UPLOADED_IMAGE_PREFIX_RE.match(card.image_filename):
            old_path = os.path.join(app.config['MACHINE_IMAGES_FOLDER'], card.image_filename)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception as e:
                    print(f"Error deleting old machine image: {e}")
        card.image_filename = new_image_filename

    db.session.commit()
    flash('Машината беше обновена успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_machine_card_window', card_id=card_id, saved='1'))
    return redirect(redirect_target)


@app.route('/services/machine-cards/<int:card_id>/delete', methods=['POST'])
@login_required
def delete_machine_card(card_id):
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    card = ServiceMachineCard.query.get_or_404(card_id)
    redirect_target = _machine_card_home(card.page)
    db.session.delete(card)
    db.session.commit()
    flash('Машината беше премахната успешно.', 'success')
    return redirect(redirect_target)


@app.route('/content-text/edit')
@login_required
def edit_text_window():
    """
    Popup edit window for any get_text() block on any public page (see
    templates/partials/editable.html). key/default come from the pencil's link -
    the default is the template's hardcoded fallback text, shown pre-filled the
    first time a given key is edited (before any EditableText row exists for it).
    """
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    key = request.args.get('key', '')
    if not key:
        flash('Липсва ключ на текста.', 'danger')
        return redirect(url_for('dashboard'))
    value = get_text(key, request.args.get('default', ''))
    return render_template(
        'edit_window.html', item_label='текст', saved=request.args.get('saved') == '1',
        action=url_for('update_text', key=key),
        fields=[{'name': 'content', 'label': 'Текст', 'value': value, 'type': 'textarea'}]
    )


@app.route('/content-text/<path:key>/update', methods=['POST'])
@login_required
def update_text(key):
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    content = request.form.get('content', '').strip()
    row = db.session.get(EditableText, key)
    if row:
        row.content = content
    else:
        db.session.add(EditableText(key=key, content=content))
    db.session.commit()
    flash('Текстът беше запазен успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_text_window', key=key, saved='1'))
    return redirect(request.referrer or url_for('index'))


@app.route('/admin/create_user', methods=['POST'])
@login_required
def admin_create_user():
    if not current_user.is_admin: return jsonify({'error': 'Неоторизиран достъп'}), 403
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()

    if not username or not password:
        flash('Попълнете всички полета.')
        return redirect(url_for('admin_users'))

    if User.query.filter_by(username=username).first():
        flash('Потребителското име вече съществува.')
        return redirect(url_for('admin_users'))

    role = request.form.get('role', 'regular_user')
    if role not in ('regular_user', 'worker', 'admin', 'web_designer'):
        flash('Невалидна роля.', 'danger')
        return redirect(url_for('admin_users'))

    secure_pass = generate_password_hash(password, method='scrypt')
    new_user = User(username=username, password=secure_pass, role=role)
    db.session.add(new_user)
    db.session.commit()
    flash(f'Успешно създаден потребител: {username}')
    return redirect(url_for('admin_users'))


@app.route('/admin/update_role/<int:user_id>', methods=['POST'])
@login_required
def admin_update_user_role(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Неоторизиран достъп'}), 403

    if user_id == current_user.id:
        flash('Не можете да променяте собствената си роля.', 'danger')
        return redirect(url_for('admin_users'))

    role = request.form.get('role', '')
    if role not in ('regular_user', 'worker', 'admin', 'web_designer'):
        flash('Невалидна роля.', 'danger')
        return redirect(url_for('admin_users'))

    user_to_update = User.query.get_or_404(user_id)
    user_to_update.role = role
    db.session.commit()
    flash(f'Ролята на {user_to_update.username} беше обновена успешно.', 'success')
    return redirect(url_for('admin_users'))


def _parse_sheet_dimensions(form):
    """
    Reads the optional sheet_length_mm/sheet_width_mm/thickness_mm fields.
    All three are optional (blank -> None) since not every material entry
    represents a specific stock size. Raises ValueError on non-numeric input,
    same as the required cost fields, so callers can catch it in one place.
    """
    values = []
    for field in ('sheet_length_mm', 'sheet_width_mm', 'thickness_mm'):
        raw = form.get(field, '').strip()
        values.append(float(raw) if raw else None)
        if values[-1] is not None and values[-1] < 0:
            raise ValueError(f'{field} must not be negative')
    return values


def _next_erp_number():
    """
    Auto-generates the next unique ERP №: one past the current max across
    Detail/Product/MaterialPrice combined (100001 if none exist yet).
    ponytail: read-then-use rather than a real DB sequence/lock - fine for
    this app's single-admin-at-a-time usage; add a proper sequence if
    concurrent creates ever start racing on this.
    """
    maxes = [
        db.session.query(db.func.max(Detail.erp_number)).scalar(),
        db.session.query(db.func.max(Product.erp_number)).scalar(),
        db.session.query(db.func.max(MaterialPrice.erp_number)).scalar(),
    ]
    current_max = max((m for m in maxes if m is not None), default=100000)
    return current_max + 1


def _parse_erp_number(form):
    """
    Reads the optional erp_number field as an int. Blank -> auto-generates
    the next unique one (see _next_erp_number) instead of leaving it unset,
    so every Detail/Product/MaterialPrice ends up with an ERP № without
    forcing admins to invent a number by hand - typing one in still
    overrides the auto value. Raises ValueError on non-integer input, same
    pattern as _parse_sheet_dimensions.
    """
    raw = form.get('erp_number', '').strip()
    return int(raw) if raw else _next_erp_number()


def _parse_material_type(form):
    """Reads the material 'type' dropdown, defaulting to 'sheets' for blank/unknown values."""
    raw = form.get('type', '').strip()
    return raw if raw in MATERIAL_TYPE_LABELS else 'sheets'


def _erp_number_conflict(erp_number, exclude_type=None, exclude_id=None):
    """
    ERP № must be unique across Detail/Product/MaterialPrice combined, not
    just within one table - a scanned barcode has to resolve to exactly
    one record (see erp_lookup()). Returns a human-readable description of
    whichever row already owns `erp_number`, or None if it's free.
    exclude_type/exclude_id skip a row's own unchanged value when editing.
    """
    if erp_number is None:
        return None

    detail_q = Detail.query.filter(Detail.erp_number == erp_number)
    product_q = Product.query.filter(Product.erp_number == erp_number)
    material_q = MaterialPrice.query.filter(MaterialPrice.erp_number == erp_number)

    if exclude_type == 'detail':
        detail_q = detail_q.filter(Detail.id != exclude_id)
    elif exclude_type == 'product':
        product_q = product_q.filter(Product.id != exclude_id)
    elif exclude_type == 'material':
        material_q = material_q.filter(MaterialPrice.id != exclude_id)

    row = detail_q.first()
    if row:
        return f'детайл "{row.name}"'
    row = product_q.first()
    if row:
        return f'продукт "{row.name}"'
    row = material_q.first()
    if row:
        return f'материал "{row.display_name}"'
    return None


@app.route('/admin/update_material/<string:key>', methods=['POST'])
@login_required
def admin_update_material(key):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    material = MaterialPrice.query.filter_by(key=key).first_or_404()

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        cost_per_meter_cut = float(request.form.get('cost_per_meter_cut', ''))
        cost_per_pierce = float(request.form.get('cost_per_pierce', ''))
        sheet_length_mm, sheet_width_mm, thickness_mm = _parse_sheet_dimensions(request.form)
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        flash('Всички цени, размери и ERP № трябва да бъдат валидни числа.', 'danger')
        return redirect(url_for('admin_materials'))

    if cost_per_m2 < 0 or cost_per_meter_cut < 0 or cost_per_pierce < 0:
        flash('Цените не могат да бъдат отрицателни числа.', 'danger')
        return redirect(url_for('admin_materials'))

    conflict = _erp_number_conflict(erp_number, exclude_type='material', exclude_id=material.id)
    if conflict:
        flash(f'ERP № {erp_number} вече се използва от {conflict}.', 'danger')
        return redirect(url_for('admin_materials'))

    # Round to 2 decimals - keeps prices in a simple, everyday currency
    # format rather than accumulating long float tails over repeated edits.
    material.cost_per_m2 = round(cost_per_m2, 2)
    material.cost_per_meter_cut = round(cost_per_meter_cut, 2)
    material.cost_per_pierce = round(cost_per_pierce, 2)
    material.sheet_length_mm = sheet_length_mm
    material.sheet_width_mm = sheet_width_mm
    material.thickness_mm = thickness_mm
    material.erp_number = erp_number
    material.code_number = request.form.get('code_number', '').strip() or None
    material.type = _parse_material_type(request.form)
    material.brand = request.form.get('brand', '').strip() or None
    db.session.commit()

    flash(f'Цените за "{material.display_name}" бяха обновени успешно.', 'success')
    return redirect(url_for('admin_materials'))


@app.route('/admin/add_material', methods=['POST'])
@login_required
def admin_add_material():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    display_name = request.form.get('display_name', '').strip()
    if not display_name:
        flash('Моля въведете име на материала.', 'danger')
        return redirect(url_for('admin_materials'))

    if MaterialPrice.query.filter_by(display_name=display_name).first():
        # Lets your boss add e.g. "Алуминий 2мм" and "Алуминий 10мм" as
        # distinct priced entries, while still catching accidental exact
        # duplicates of the same name.
        flash(f'Вече съществува материал с име "{display_name}".', 'danger')
        return redirect(url_for('admin_materials'))

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        cost_per_meter_cut = float(request.form.get('cost_per_meter_cut', ''))
        cost_per_pierce = float(request.form.get('cost_per_pierce', ''))
        sheet_length_mm, sheet_width_mm, thickness_mm = _parse_sheet_dimensions(request.form)
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        flash('Всички цени, размери и ERP № трябва да бъдат валидни числа.', 'danger')
        return redirect(url_for('admin_materials'))

    if cost_per_m2 < 0 or cost_per_meter_cut < 0 or cost_per_pierce < 0:
        flash('Цените не могат да бъдат отрицателни числа.', 'danger')
        return redirect(url_for('admin_materials'))

    conflict = _erp_number_conflict(erp_number)
    if conflict:
        flash(f'ERP № {erp_number} вече се използва от {conflict}.', 'danger')
        return redirect(url_for('admin_materials'))

    # The key is just an opaque internal identifier (used in DxfFile.material
    # and the dashboard <select> value) - it's never shown to users, so a
    # simple auto-generated id-based key avoids any need to transliterate
    # Cyrillic display names into a URL-safe slug.
    new_material = MaterialPrice(
        key='pending',  # placeholder, replaced with a real unique key below
        display_name=display_name,
        cost_per_m2=round(cost_per_m2, 2),
        cost_per_meter_cut=round(cost_per_meter_cut, 2),
        cost_per_pierce=round(cost_per_pierce, 2),
        sheet_length_mm=sheet_length_mm,
        sheet_width_mm=sheet_width_mm,
        thickness_mm=thickness_mm,
        erp_number=erp_number,
        code_number=request.form.get('code_number', '').strip() or None,
        type=_parse_material_type(request.form),
        brand=request.form.get('brand', '').strip() or None
    )
    db.session.add(new_material)
    db.session.flush()  # assigns new_material.id without a full commit yet
    new_material.key = f'material_{new_material.id}'
    db.session.commit()

    flash(f'Материалът "{display_name}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_materials'))


@app.route('/admin/products/<int:product_id>/upload_image', methods=['POST'])
@login_required
def admin_product_upload_image(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    if 'images' not in request.files:
        flash('Няма избрани файлове.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    files = request.files.getlist('images')
    uploaded_count = 0
    allowed_extensions = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

    for file in files:
        if file and file.filename != '':
            ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
            if ext in allowed_extensions:
                filename = secure_filename(file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                file_path = os.path.join(app.config['PRODUCT_IMAGES_FOLDER'], unique_filename)
                file.save(file_path)

                new_img = ProductImage(product_id=product.id, filename=unique_filename)
                db.session.add(new_img)
                uploaded_count += 1
            else:
                flash(f'Невалиден формат на файла: {file.filename}. Разрешени са само изображения.', 'danger')

    if uploaded_count > 0:
        db.session.commit()
        flash(f'Успешно качени изображения: {uploaded_count} бр.', 'success')

    return redirect(url_for('admin_product_edit', product_id=product.id))



@app.route('/machines/add', methods=['POST'])
@login_required
def add_machine():
    if not (current_user.is_admin or current_user.can_edit_content):
        flash("Нямате права да добавяте машини.", "danger")
        return redirect(url_for('list_machines'))

    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на машината.', 'danger')
        return redirect(url_for('list_machines'))

    new_machine = Machine(name=name)
    db.session.add(new_machine)
    db.session.commit()
    flash('Машината е добавена успешно!', 'success')
    return redirect(url_for('list_machines'))


@app.route('/machines/<int:id>/edit')
@login_required
def edit_machine_window(id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /machines."""
    if not (current_user.is_admin or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    machine = Machine.query.get_or_404(id)
    return render_template(
        'edit_window.html', item_label='машина', saved=request.args.get('saved') == '1',
        action=url_for('rename_machine', id=machine.id),
        fields=[{'name': 'name', 'label': 'Име на машина', 'value': machine.name, 'type': 'text', 'required': True}]
    )


@app.route('/machines/<int:id>/rename', methods=['POST'])
@login_required
def rename_machine(id):
    if not (current_user.is_admin or current_user.can_edit_content):
        flash("Нямате права да преименувате машини.", "danger")
        return redirect(url_for('list_machines'))

    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на машината.', 'danger')
        return redirect(url_for('list_machines'))

    machine = Machine.query.get_or_404(id)
    machine.name = name
    db.session.commit()
    flash('Машината беше преименувана успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_machine_window', id=id, saved='1'))
    return redirect(url_for('list_machines'))


@app.route('/machines/update/<int:id>', methods=['POST'])
@login_required
def update_machine_status(id):
    # Workers or Admins can update status
    if current_user.role not in ['admin', 'worker']:
        return "Unauthorized", 403

    status = request.form.get('status', '')
    if status not in ('idle', 'running', 'maintenance'):
        return "Invalid status", 400

    machine = Machine.query.get_or_404(id)
    machine.status = status
    db.session.commit()
    flash(f'Статусът на {machine.name} е актуализиран.', 'success')
    return redirect(url_for('list_machines'))

@app.route('/machines')
@login_required
def list_machines():
    if not (current_user.is_staff or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    machines = Machine.query.all()
    return render_template('machines.html', machines=machines, active_page='machines')

@app.route('/admin/products/<int:product_id>/delete_image/<int:image_id>', methods=['POST'])
@login_required
def admin_product_delete_image(product_id, image_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    img = ProductImage.query.filter_by(id=image_id, product_id=product_id).first_or_404()
    file_path = os.path.join(app.config['PRODUCT_IMAGES_FOLDER'], img.filename)

    # Remove asset from filesystem to prevent dead bytes accumulation
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        print(f"Error deleting file from disk: {e}")

    db.session.delete(img)
    db.session.commit()
    flash('Изображението беше премахнато.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product_id))


# ----------------- БИБЛИОТЕКА С ДЕТАЙЛИ (Detail catalog) -----------------

@app.route('/admin/details/add', methods=['POST'])
@login_required
def admin_add_detail():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    name = request.form.get('name', '').strip()
    material_key = request.form.get('material', '')
    try:
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        flash('ERP № трябва да бъде цяло число.', 'danger')
        return redirect(url_for('admin_details'))
    code_number = request.form.get('code_number', '').strip() or None

    if not name:
        flash('Моля въведете име на детайла.', 'danger')
        return redirect(url_for('admin_details'))

    conflict = _erp_number_conflict(erp_number)
    if conflict:
        flash(f'ERP № {erp_number} вече се използва от {conflict}.', 'danger')
        return redirect(url_for('admin_details'))

    if not MaterialPrice.query.filter_by(key=material_key).first():
        flash('Невалиден избор на материал.', 'danger')
        return redirect(url_for('admin_details'))

    if 'file' not in request.files or request.files['file'].filename == '':
        flash('Моля качете .dxf файл за детайла.', 'danger')
        return redirect(url_for('admin_details'))

    file = request.files['file']
    if not file.filename.lower().endswith('.dxf'):
        flash('Невалиден формат! Приемат се само .dxf файлове.', 'danger')
        return redirect(url_for('admin_details'))

    temp_path = None
    try:
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
        file.save(temp_path)

        width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(temp_path)
        if width is None:
            flash('Грешка при обработката на DXF структурата.', 'danger')
            return redirect(url_for('admin_details'))

        price = calculate_cnc_price(width, height, total_length, pierce_count, material_key)

        new_detail = Detail(
            name=name, material_key=material_key, width=width, height=height,
            total_length=total_length, pierce_count=pierce_count,
            calculated_price=price, geometry_json=json.dumps(shapes),
            erp_number=erp_number, code_number=code_number
        )
        db.session.add(new_detail)
        db.session.commit()
        flash(f'Детайлът "{name}" беше добавен успешно.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при обработка/запис: {str(e)}', 'danger')
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    return redirect(url_for('admin_details'))


@app.route('/admin/details/<int:detail_id>/edit')
@login_required
def edit_detail_window(detail_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/content."""
    if not (current_user.is_admin or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    detail = Detail.query.get_or_404(detail_id)
    return render_template(
        'edit_window.html', item_label='детайл', saved=request.args.get('saved') == '1',
        action=url_for('admin_rename_detail', detail_id=detail.id),
        fields=[{'name': 'name', 'label': 'Име на детайла', 'value': detail.name, 'type': 'text', 'required': True}]
    )


@app.route('/admin/details/<int:detail_id>/rename', methods=['POST'])
@login_required
def admin_rename_detail(detail_id):
    if not (current_user.is_admin or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на детайла.', 'danger')
        return redirect(url_for('admin_content'))

    detail = Detail.query.get_or_404(detail_id)
    detail.name = name
    db.session.commit()
    flash('Детайлът беше преименуван успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_detail_window', detail_id=detail_id, saved='1'))
    return redirect(url_for('admin_content'))


@app.route('/admin/details/delete/<int:detail_id>', methods=['POST'])
@login_required
def admin_delete_detail(detail_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    detail = Detail.query.get_or_404(detail_id)

    # A detail used inside any product can't be deleted out from under it -
    # that would silently corrupt that product's price. Remove it from every
    # product first (via the product edit page), then delete it here.
    if ProductDetail.query.filter_by(detail_id=detail.id).first():
        flash(f'Детайлът "{detail.name}" се използва в поне един продукт и не може да бъде изтрит.', 'danger')
        return redirect(url_for('admin_details'))

    db.session.delete(detail)
    db.session.commit()
    flash(f'Детайлът "{detail.name}" беше изтрит.', 'success')
    return redirect(url_for('admin_details'))




# ----------------- ПРОДУКТИ (Products) -----------------

@app.route('/admin/products/add', methods=['POST'])
@login_required
def admin_add_product():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на продукта.', 'danger')
        return redirect(url_for('admin_products'))

    description = request.form.get('description', '').strip()

    try:
        markup_percent = float(request.form.get('markup_percent', '0') or 0)
    except ValueError:
        markup_percent = 0.0

    new_product = Product(name=name, description=description, markup_percent=round(markup_percent, 2))
    db.session.add(new_product)
    db.session.commit()

    flash(f'Продуктът "{name}" беше създаден. Добавете детайли и допълнителни разходи по-долу.', 'success')
    return redirect(url_for('admin_product_edit', product_id=new_product.id))


@app.route('/admin/products/<int:product_id>/edit')
@login_required
def admin_product_edit(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    all_details = Detail.query.order_by(Detail.name).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    pricing = calculate_product_pricing(product)
    return render_template('product_edit.html', product=product, all_details=all_details, pricing=pricing, materials=materials, active_page='admin')


@app.route('/admin/products/<int:product_id>/edit-content')
@login_required
def edit_product_content_window(product_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/content."""
    if not (current_user.is_admin or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    product = Product.query.get_or_404(product_id)
    return render_template(
        'edit_window.html', item_label='продукт', saved=request.args.get('saved') == '1',
        action=url_for('admin_product_update', product_id=product.id),
        fields=[
            {'name': 'name', 'label': 'Име на продукта', 'value': product.name, 'type': 'text', 'required': True},
            {'name': 'description', 'label': 'Описание', 'value': product.description or '', 'type': 'textarea'},
        ]
    )


@app.route('/admin/products/<int:product_id>/update', methods=['POST'])
@login_required
def admin_product_update(product_id):
    if not (current_user.is_admin or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    is_popup = request.form.get('popup') == '1'
    # Popup (content-only) submissions never touch pricing/ERP, even from an admin -
    # that form only ever carries name/description. The admin-only fields below are
    # exclusive to the full admin_product_edit page.
    edit_pricing = current_user.is_admin and not is_popup

    if is_popup:
        redirect_target = url_for('edit_product_content_window', product_id=product_id, saved='1')
    elif current_user.is_admin:
        redirect_target = url_for('admin_product_edit', product_id=product_id)
    else:
        redirect_target = url_for('admin_content')

    product = Product.query.get_or_404(product_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на продукта.', 'danger')
        return redirect(redirect_target)

    product.name = name
    product.description = request.form.get('description', '').strip()

    if edit_pricing:
        try:
            markup_percent = float(request.form.get('markup_percent', '0') or 0)
            erp_number = _parse_erp_number(request.form)
        except ValueError:
            flash('Надценката и ERP № трябва да бъдат валидни числа.', 'danger')
            return redirect(redirect_target)

        conflict = _erp_number_conflict(erp_number, exclude_type='product', exclude_id=product.id)
        if conflict:
            flash(f'ERP № {erp_number} вече се използва от {conflict}.', 'danger')
            return redirect(redirect_target)

        product.markup_percent = round(markup_percent, 2)
        product.erp_number = erp_number
        product.code_number = request.form.get('code_number', '').strip() or None

    db.session.commit()

    flash('Продуктът беше обновен успешно.', 'success')
    return redirect(redirect_target)


@app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
@login_required
def admin_product_delete(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    name = product.name

    # Unlink files from storage before cascading database removal
    for img in product.images:
        file_path = os.path.join(app.config['PRODUCT_IMAGES_FOLDER'], img.filename)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Failed disk cleanup for file {img.filename}: {e}")

    db.session.delete(product)  # Cascades database records
    db.session.commit()
    flash(f'Продуктът "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_products'))


@app.route('/admin/products/<int:product_id>/add_detail', methods=['POST'])
@login_required
def admin_product_add_detail(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    try:
        detail_id = int(request.form.get('detail_id', ''))
        quantity = int(request.form.get('quantity', '1'))
    except ValueError:
        flash('Невалиден детайл или количество.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    if quantity < 1:
        flash('Количеството трябва да бъде поне 1.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    detail = Detail.query.get_or_404(detail_id)

    # If this detail is already on the product, just bump its quantity
    # instead of creating a duplicate line item.
    existing = ProductDetail.query.filter_by(product_id=product.id, detail_id=detail.id).first()
    if existing:
        existing.quantity += quantity
    else:
        db.session.add(ProductDetail(product_id=product.id, detail_id=detail.id, quantity=quantity))

    db.session.commit()
    flash(f'Детайлът "{detail.name}" беше добавен към продукта.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product.id))


@app.route('/admin/products/<int:product_id>/remove_detail/<int:product_detail_id>', methods=['POST'])
@login_required
def admin_product_remove_detail(product_id, product_detail_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    line_item = ProductDetail.query.filter_by(id=product_detail_id, product_id=product_id).first_or_404()
    db.session.delete(line_item)
    db.session.commit()
    flash('Детайлът беше премахнат от продукта.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product_id))


@app.route('/admin/products/<int:product_id>/add_cost', methods=['POST'])
@login_required
def admin_product_add_cost(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    label = request.form.get('label', '').strip()

    try:
        amount = float(request.form.get('amount', ''))
    except ValueError:
        flash('Сумата трябва да бъде валидно число.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    if not label:
        flash('Моля въведете описание на разхода.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    if amount < 0:
        flash('Сумата не може да бъде отрицателна.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    db.session.add(ProductExtraCost(product_id=product.id, label=label, amount=round(amount, 2)))
    db.session.commit()
    flash(f'Разходът "{label}" беше добавен.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product.id))


@app.route('/admin/products/<int:product_id>/remove_cost/<int:cost_id>', methods=['POST'])
@login_required
def admin_product_remove_cost(product_id, cost_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    cost = ProductExtraCost.query.filter_by(id=cost_id, product_id=product_id).first_or_404()
    db.session.delete(cost)
    db.session.commit()
    flash('Разходът беше премахнат.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product_id))


@app.route('/admin/products/<int:product_id>/offer')
@login_required
def admin_product_offer(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    pricing = calculate_product_pricing(product)
    customer_name = request.args.get('customer', '')
    clients = Client.query.order_by(Client.name).all()
    return render_template('offer.html', product=product, pricing=pricing, customer_name=customer_name, clients=clients)


@app.route('/admin/products/<int:product_id>/protocol')
@login_required
def admin_product_protocol(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    clients = Client.query.order_by(Client.name).all()
    return render_template('protocol.html', product=product, clients=clients)


@app.route('/admin/products/<int:product_id>/certificates')
@login_required
def admin_product_certificates(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    clients = Client.query.order_by(Client.name).all()
    return render_template('certificate.html', product=product, clients=clients)


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if not current_user.is_admin:
        flash('Нямате администраторски права!', 'danger')
        return redirect(url_for('dashboard'))

    user_to_delete = User.query.get_or_404(user_id)

    # Defense in depth: the UI hides this button for your own account, but
    # guard against a directly crafted request too.
    if user_to_delete.id == current_user.id:
        flash('Не можете да изтриете собствения си профил оттук.', 'danger')
        return redirect(url_for('admin_users'))

    try:
        # Note: uploaded DXF files are only ever written temporarily during
        # processing and removed immediately after (see dashboard()) - only
        # the extracted metrics/geometry persist in the DB. So there are no
        # leftover files on disk to clean up here; deleting the user cascades
        # to their DxfFile rows via the model's cascade='all, delete-orphan'.
        db.session.delete(user_to_delete)
        db.session.commit()

        flash(f'Потребителят {user_to_delete.username} и неговите чертежи бяха изтрити!', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване на данни: {str(e)}', 'danger')

    return redirect(url_for('admin_users'))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# МАРШРУТ ЗА ОБИКНОВЕНИ ПОТРЕБИТЕЛИ - СЪЗДАВАНЕ НА ПОРЪЧКА (кошница с няколко артикула)
@app.route('/orders/new', methods=['GET', 'POST'])
@login_required
def create_order():
    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '').strip()
        cart_raw = request.form.get('cart_json', '')

        if not customer_name:
            flash('Моля въведете име на клиент.', 'danger')
            return redirect(url_for('create_order'))

        try:
            cart = json.loads(cart_raw)
            if not isinstance(cart, list):
                cart = []
        except (TypeError, ValueError):
            cart = []

        if not cart:
            flash('Моля добавете поне един артикул към поръчката.', 'danger')
            return redirect(url_for('create_order'))

        machine_id_raw = request.form.get('machine_id', '')
        machine_id = int(machine_id_raw) if machine_id_raw and machine_id_raw.isdigit() else None
        client_id_raw = request.form.get('client_id', '')
        client_id = int(client_id_raw) if client_id_raw and client_id_raw.isdigit() else None
        deliverer_id_raw = request.form.get('deliverer_id', '')
        deliverer_id = int(deliverer_id_raw) if deliverer_id_raw and deliverer_id_raw.isdigit() else None

        new_order = Order(
            order_number=generate_order_number(),
            user_id=current_user.id,
            customer_name=customer_name,
            status='new',
            machine_id=machine_id,
            client_id=client_id,
            deliverer_id=deliverer_id
        )
        db.session.add(new_order)
        db.session.flush()  # Взимаме ID-то преди commit

        added_any = False
        for row in cart:
            if not isinstance(row, dict):
                continue
            try:
                item_type = row.get('type')
                item_id = int(row.get('id'))
                qty = int(row.get('qty', 1))
            except (TypeError, ValueError):
                continue
            if qty < 1:
                continue

            if item_type == 'product':
                product = Product.query.get(item_id)
                if not product:
                    continue
                pricing = calculate_product_pricing(product)
                order_item = OrderItem(
                    order_id=new_order.id, product_id=product.id,
                    quantity_ordered=qty, unit_price=pricing['sell_price']
                )
                db.session.add(order_item)
                db.session.flush()  # need order_item.id for its components

                # Freeze the product's current recipe into per-detail
                # production targets for this order line.
                for pd in product.product_details:
                    db.session.add(OrderItemComponent(
                        order_item_id=order_item.id,
                        detail_id=pd.detail_id,
                        detail_name_snapshot=pd.detail.name,
                        quantity_needed=pd.quantity * qty
                    ))
                added_any = True

            elif item_type == 'detail':
                detail = Detail.query.get(item_id)
                if not detail:
                    continue
                order_item = OrderItem(
                    order_id=new_order.id, detail_id=detail.id,
                    quantity_ordered=qty, unit_price=detail.calculated_price
                )
                db.session.add(order_item)
                added_any = True

        if not added_any:
            db.session.rollback()
            flash('Невалидни артикули в поръчката.', 'danger')
            return redirect(url_for('create_order'))

        db.session.commit()
        flash(f'Поръчка {new_order.order_number} беше успешно изпратена!', 'success')

        shortfalls = order_missing_items(new_order)
        if shortfalls:
            missing_desc = '; '.join(
                f"{s['item_name']} (нужни {s['needed']}, налични {s['available']})" for s in shortfalls
            )
            flash(
                f'Внимание: поръчка {new_order.order_number} има недостатъчна наличност за: {missing_desc}. '
                'Виж таблото "Липсваща наличност".',
                'danger'
            )
        return redirect(url_for('my_orders'))

    products = Product.query.order_by(Product.name).all()
    details = Detail.query.order_by(Detail.name).all()
    machines = Machine.query.order_by(Machine.name).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    clients = Client.query.order_by(Client.name).all()
    deliverers = Deliverer.query.order_by(Deliverer.name).all()
    # Pre-computed, JSON-friendly catalogs so the cart UI can add items and
    # show live prices/totals client-side without extra round-trips.
    products_data = [
        {'id': p.id, 'name': p.name, 'price': calculate_product_pricing(p)['sell_price']}
        for p in products
    ]
    details_data = [
        {
            'id': d.id,
            'name': f"{d.name} ({d.material.display_name})" if d.material else d.name,
            'price': d.calculated_price
        }
        for d in details
    ]
    return render_template('order_create.html', products=products_data, details=details_data,
                           machines=machines, materials=materials, clients=clients, deliverers=deliverers,
                           active_page='create_order')


# МАРШРУТ ЗА ОБИКНОВЕНИ ПОТРЕБИТЕЛИ - ИСТОРИЯ И СТАТУС НА СОБСТВЕНИТЕ ПОРЪЧКИ
@app.route('/orders')
@login_required
def my_orders():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template('my_orders.html', orders=orders, active_page='my_orders')


@app.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_order(order_id):
    order = Order.query.get_or_404(order_id)

    if order.user_id != current_user.id and not current_user.is_admin:
        flash('Нямате достъп до тази поръчка.', 'danger')
        return redirect(url_for('my_orders'))

    if not order.can_cancel:
        flash('Поръчката вече е в процес на изработка (или вече е приключена/отменена) и не може да бъде отменена.',
              'danger')
        return redirect(url_for('my_orders'))

    order.status = 'cancelled'
    db.session.commit()
    flash(f'Поръчка {order.order_number} беше отменена.', 'success')
    return redirect(url_for('my_orders'))


# АДМИН СТРАНИЦА - СПРАВКА ЗА ПРОИЗВОДСТВО И ОСТАТЪЦИ
@app.route('/admin/production', methods=['GET', 'POST'])
@login_required
def admin_production_report():
    if not current_user.is_staff:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        # Динамично обновяване на изработеното количество - или на цял
        # OrderItem (за самостоятелен детайл), или на един конкретен
        # компонент (детайл) от продукт (target_type = 'item' / 'component').
        target_type = request.form.get('target_type')
        target_id = request.form.get('target_id')

        try:
            produced_qty = int(request.form.get('produced_qty', 0))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'Невалидно количество.'}), 400

        if target_type == 'component':
            component = OrderItemComponent.query.get_or_404(target_id)
            produced_qty = max(0, min(produced_qty, component.quantity_needed))
            component.quantity_produced = produced_qty
            order_item = component.order_item
            target_percent = component.percent_complete
        elif target_type == 'item':
            order_item = OrderItem.query.get_or_404(target_id)
            produced_qty = max(0, min(produced_qty, order_item.quantity_ordered))
            order_item.quantity_produced = produced_qty
            target_percent = order_item.percent_complete
        else:
            return jsonify({'status': 'error', 'message': 'Невалиден тип на артикула.'}), 400

        order = order_item.order
        refresh_order_status(order)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'produced_qty': produced_qty,
            'target_percent': target_percent,
            'item_percent': order_item.percent_complete,
            'order_percent': order.percent_complete,
            'order_status': order.status,
            'order_status_label': STATUS_LABELS.get(order.status, order.status)
        })

    orders = Order.query.filter(Order.status != 'cancelled').order_by(Order.created_at.desc()).all()
    machines = Machine.query.order_by(Machine.name).all()
    return render_template('production_report.html', orders=orders, machines=machines, active_page='production')


@app.route('/admin/missing-stock')
@role_required(['admin', 'worker'])
def admin_missing_stock():
    """
    Dashboard for admins/workers: every open (not completed/cancelled) order
    that currently doesn't have enough Detail/Product stock to fulfill it -
    see order_missing_items() for what "enough" means. Recomputed live on
    every load rather than stored, so it's never stale.
    """
    open_orders = Order.query.filter(
        Order.status.in_(['new', 'in_production'])
    ).order_by(Order.created_at.desc()).all()

    orders_with_shortfalls = []
    for order in open_orders:
        shortfalls = order_missing_items(order)
        if shortfalls:
            orders_with_shortfalls.append({'order': order, 'shortfalls': shortfalls})

    return render_template(
        'admin_missing_stock.html', orders_with_shortfalls=orders_with_shortfalls,
        active_page='admin_missing_stock'
    )


def generate_barcode_svg(code):
    """
    Renders `code` as an inline Code128 SVG barcode string for the label
    print page. Uses python-barcode (small, well-tested, pure-Python)
    instead of hand-rolling the Code128 bit tables or pulling a JS barcode
    library from a CDN - this app has no other runtime CDN dependency.
    """
    buf = io.BytesIO()
    barcode.get('code128', str(code), writer=SVGWriter()).write(
        buf, options={'write_text': False, 'module_height': 8, 'quiet_zone': 1}
    )
    svg = buf.getvalue().decode('utf-8')
    return svg[svg.index('<svg'):]  # drop the XML prolog/doctype so it can be embedded inline


@app.route('/admin/print-label/<string:target_type>/<int:target_id>')
@role_required(['admin', 'worker'])
def print_label(target_type, target_id):
    """
    Renders a printable label (see label.html) for one of:
    - 'item' / 'component': a produced batch on an order (standalone-Detail
      OrderItem or one Product's OrderItemComponent) - quantity comes from
      quantity_produced, same target_type/target_id convention as
      admin_production_report's POST handler above.
    - 'detail' / 'product' / 'material': a catalog entry printed on its own,
      with no order context - quantity comes from the ?quantity= query
      param (a plain GET param since this is a browser-navigated print
      page, not a form post).
    """
    order = None
    quantity = None
    # The entity that actually owns erp_number/code_number and can be
    # quick-edited from the label page - for an order item/component this
    # is the linked Detail, not the order row itself.
    edit_target_type = None
    edit_target_id = None

    if target_type == 'component':
        row = OrderItemComponent.query.get_or_404(target_id)
        name = row.detail_name_snapshot
        quantity = row.quantity_produced
        order = row.order_item.order
        erp_number = row.detail.erp_number if row.detail else None
        code_number = row.detail.code_number if row.detail else None
        if row.detail:
            edit_target_type, edit_target_id = 'detail', row.detail.id
    elif target_type == 'item':
        row = OrderItem.query.get_or_404(target_id)
        if not row.detail:
            flash('Този артикул е продукт, а не самостоятелен детайл - етикет не може да бъде отпечатан за него.', 'danger')
            return redirect(url_for('admin_production_report'))
        name = row.detail.name
        quantity = row.quantity_produced
        order = row.order
        erp_number = row.detail.erp_number
        code_number = row.detail.code_number
        edit_target_type, edit_target_id = 'detail', row.detail.id
    elif target_type == 'detail':
        row = Detail.query.get_or_404(target_id)
        name = row.name
        erp_number = row.erp_number
        code_number = row.code_number
        edit_target_type, edit_target_id = 'detail', row.id
    elif target_type == 'product':
        row = Product.query.get_or_404(target_id)
        name = row.name
        erp_number = row.erp_number
        code_number = row.code_number
        edit_target_type, edit_target_id = 'product', row.id
    elif target_type == 'material':
        row = MaterialPrice.query.get_or_404(target_id)
        name = row.display_name
        erp_number = row.erp_number
        code_number = row.code_number
        edit_target_type, edit_target_id = 'material', row.id
    else:
        flash('Невалиден тип етикет.', 'danger')
        return redirect(url_for('admin_dashboard'))

    return_quantity = None
    if quantity is None:
        return_quantity = request.args.get('quantity', 1, type=int) or 1
        quantity = max(1, return_quantity)

    barcode_svg = generate_barcode_svg(erp_number) if erp_number else None

    return render_template(
        'label.html', order=order, erp_number=erp_number, code_number=code_number,
        name=name, quantity=quantity, barcode_svg=barcode_svg,
        print_date=datetime.now().strftime('%d/%m/%Y'),
        edit_target_type=edit_target_type, edit_target_id=edit_target_id,
        return_target_type=target_type, return_target_id=target_id, return_quantity=return_quantity
    )


@app.route('/admin/print-label/<string:edit_target_type>/<int:edit_target_id>/update-codes', methods=['POST'])
@role_required(['admin', 'worker'])
def update_label_codes(edit_target_type, edit_target_id):
    """
    Quick-edit for ERP №/КД № directly from the label print page, so you
    don't have to leave it and go back to the admin panel just to fill
    those in before printing. Always edits the underlying Detail/Product/
    MaterialPrice row - for an order item/component label that's the
    linked Detail (see edit_target_type in print_label() above), never the
    order row itself.
    """
    if edit_target_type == 'detail':
        row = Detail.query.get_or_404(edit_target_id)
    elif edit_target_type == 'product':
        row = Product.query.get_or_404(edit_target_id)
    elif edit_target_type == 'material':
        row = MaterialPrice.query.get_or_404(edit_target_id)
    else:
        flash('Невалиден тип за редакция.', 'danger')
        return redirect(url_for('admin_dashboard'))

    return_target_type = request.form.get('return_target_type', edit_target_type)
    return_target_id = request.form.get('return_target_id', edit_target_id, type=int)
    return_quantity = request.form.get('return_quantity', type=int)

    def _back_to_label():
        url = url_for('print_label', target_type=return_target_type, target_id=return_target_id)
        return url + (f'?quantity={return_quantity}' if return_quantity else '')

    try:
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        flash('ERP № трябва да бъде цяло число.', 'danger')
        return redirect(_back_to_label())

    conflict = _erp_number_conflict(erp_number, exclude_type=edit_target_type, exclude_id=edit_target_id)
    if conflict:
        flash(f'ERP № {erp_number} вече се използва от {conflict}.', 'danger')
        return redirect(_back_to_label())

    row.erp_number = erp_number
    row.code_number = request.form.get('code_number', '').strip() or None
    db.session.commit()
    flash('ERP №/КД № бяха обновени.', 'success')

    url = _back_to_label()
    return redirect(url)


@app.route('/admin/erp-lookup')
@role_required(['admin', 'worker'])
def erp_lookup():
    """
    Resolves a scanned/typed ERP № to whichever Detail/Product/
    MaterialPrice owns it and jumps straight there. ERP № is unique across
    all three tables (see _erp_number_conflict), so this always resolves
    to at most one record - the barcode itself still just encodes the bare
    number, since that's what a handheld scanner types into this search
    box, which is what makes scanning it "point to" the right record.
    """
    erp_number = request.args.get('erp_number', type=int)
    if erp_number is None:
        flash('Моля въведете валиден ERP № (цяло число).', 'danger')
        return redirect(url_for('admin_dashboard'))

    product = Product.query.filter_by(erp_number=erp_number).first()
    if product:
        return redirect(url_for('admin_product_edit', product_id=product.id))

    detail = Detail.query.filter_by(erp_number=erp_number).first()
    if detail:
        flash(f'Намерен детайл: "{detail.name}" (вижте таблицата с детайли по-долу).', 'success')
        return redirect(url_for('admin_details'))

    material = MaterialPrice.query.filter_by(erp_number=erp_number).first()
    if material:
        flash(f'Намерен материал: "{material.display_name}" (вижте таблицата с материали по-долу).', 'success')
        return redirect(url_for('admin_materials'))

    flash(f'Няма запис с ERP № {erp_number}.', 'danger')
    return redirect(url_for('admin_dashboard'))


# ----------------- QUICK-CREATE API ENDPOINTS -----------------

@app.route('/api/quick-create-detail', methods=['POST'])
@login_required
def api_quick_create_detail():
    """AJAX endpoint: create a Detail from a DXF file + material, returns JSON."""
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Нямате достъп.'}), 403

    name = request.form.get('name', '').strip()
    material_key = request.form.get('material', '')

    if not name:
        return jsonify({'status': 'error', 'message': 'Моля въведете име на детайла.'}), 400

    try:
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'ERP № трябва да бъде цяло число.'}), 400
    code_number = request.form.get('code_number', '').strip() or None

    conflict = _erp_number_conflict(erp_number)
    if conflict:
        return jsonify({'status': 'error', 'message': f'ERP № {erp_number} вече се използва от {conflict}.'}), 400

    if not MaterialPrice.query.filter_by(key=material_key).first():
        return jsonify({'status': 'error', 'message': 'Невалиден избор на материал.'}), 400

    if 'file' not in request.files or request.files['file'].filename == '':
        return jsonify({'status': 'error', 'message': 'Моля качете .dxf файл.'}), 400

    file = request.files['file']
    if not file.filename.lower().endswith('.dxf'):
        return jsonify({'status': 'error', 'message': 'Невалиден формат! Приемат се само .dxf файлове.'}), 400

    temp_path = None
    try:
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
        file.save(temp_path)

        width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(temp_path)
        if width is None:
            return jsonify({'status': 'error', 'message': 'Грешка при обработката на DXF файла.'}), 400

        price = calculate_cnc_price(width, height, total_length, pierce_count, material_key)
        mat = MaterialPrice.query.filter_by(key=material_key).first()

        new_detail = Detail(
            name=name, material_key=material_key, width=width, height=height,
            total_length=total_length, pierce_count=pierce_count,
            calculated_price=price, geometry_json=json.dumps(shapes),
            erp_number=erp_number, code_number=code_number
        )
        db.session.add(new_detail)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'detail': {
                'id': new_detail.id,
                'name': f"{new_detail.name} ({mat.display_name})",
                'price': new_detail.calculated_price
            }
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': f'Грешка: {str(e)}'}), 500
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@app.route('/api/quick-create-product', methods=['POST'])
@login_required
def api_quick_create_product():
    """
    AJAX endpoint: create a Product, optionally with its Detail components
    (BOM) attached in the same call - unlike admin_add_product(), which
    always creates a bare product and sends the admin on to
    admin_product_edit() to attach details afterward, this is the one path
    where "quick" still means picking the recipe up front. components_json
    follows [{detail_id, quantity}], same shape admin_product_add_detail()
    accepts one row at a time; duplicate detail_ids here are summed rather
    than creating two ProductDetail rows for the same detail.
    """
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Нямате достъп.'}), 403

    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'Моля въведете име на продукта.'}), 400

    description = request.form.get('description', '').strip()
    try:
        markup_percent = float(request.form.get('markup_percent', '0') or 0)
    except ValueError:
        markup_percent = 0.0

    try:
        raw_components = json.loads(request.form.get('components_json', '') or '[]')
        if not isinstance(raw_components, list):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Невалидни компоненти.'}), 400

    components = {}  # detail_id -> quantity, merging duplicates
    for row in raw_components:
        if not isinstance(row, dict):
            return jsonify({'status': 'error', 'message': 'Невалидни компоненти.'}), 400
        try:
            detail_id = int(row.get('detail_id'))
            quantity = int(row.get('quantity'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'Невалиден детайл или количество.'}), 400
        if quantity < 1:
            return jsonify({'status': 'error', 'message': 'Количеството трябва да бъде поне 1.'}), 400
        if not Detail.query.get(detail_id):
            return jsonify({'status': 'error', 'message': 'Невалиден избор на детайл.'}), 400
        components[detail_id] = components.get(detail_id, 0) + quantity

    new_product = Product(name=name, description=description, markup_percent=round(markup_percent, 2))
    db.session.add(new_product)
    db.session.flush()

    for detail_id, quantity in components.items():
        db.session.add(ProductDetail(product_id=new_product.id, detail_id=detail_id, quantity=quantity))

    db.session.commit()

    pricing = calculate_product_pricing(new_product)

    return jsonify({
        'status': 'success',
        'product': {
            'id': new_product.id,
            'name': new_product.name,
            'price': pricing['sell_price']
        }
    })


@app.route('/api/quick-create-material', methods=['POST'])
@login_required
def api_quick_create_material():
    """
    AJAX endpoint: create a MaterialPrice on the fly (same required fields
    and validation as admin_add_material()) from wherever a material
    <select> needs one that doesn't exist yet - the quick-create-detail
    modal and the delivery-note detail-material picker. Admin-only, same as
    the Detail/Product quick-create endpoints (catalog-management action).
    """
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Нямате достъп.'}), 403

    display_name = request.form.get('display_name', '').strip()
    if not display_name:
        return jsonify({'status': 'error', 'message': 'Моля въведете име на материала.'}), 400

    if MaterialPrice.query.filter_by(display_name=display_name).first():
        return jsonify({'status': 'error', 'message': f'Вече съществува материал с име "{display_name}".'}), 400

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        cost_per_meter_cut = float(request.form.get('cost_per_meter_cut', ''))
        cost_per_pierce = float(request.form.get('cost_per_pierce', ''))
        sheet_length_mm, sheet_width_mm, thickness_mm = _parse_sheet_dimensions(request.form)
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Всички цени, размери и ERP № трябва да бъдат валидни числа.'}), 400

    if cost_per_m2 < 0 or cost_per_meter_cut < 0 or cost_per_pierce < 0:
        return jsonify({'status': 'error', 'message': 'Цените не могат да бъдат отрицателни числа.'}), 400

    conflict = _erp_number_conflict(erp_number)
    if conflict:
        return jsonify({'status': 'error', 'message': f'ERP № {erp_number} вече се използва от {conflict}.'}), 400

    new_material = MaterialPrice(
        key='pending',
        display_name=display_name,
        cost_per_m2=round(cost_per_m2, 2),
        cost_per_meter_cut=round(cost_per_meter_cut, 2),
        cost_per_pierce=round(cost_per_pierce, 2),
        sheet_length_mm=sheet_length_mm,
        sheet_width_mm=sheet_width_mm,
        thickness_mm=thickness_mm,
        erp_number=erp_number,
        code_number=request.form.get('code_number', '').strip() or None,
        type=_parse_material_type(request.form),
        brand=request.form.get('brand', '').strip() or None
    )
    db.session.add(new_material)
    db.session.flush()
    new_material.key = f'material_{new_material.id}'
    db.session.commit()

    return jsonify({
        'status': 'success',
        'material': {'key': new_material.key, 'option_text': format_material_option(new_material)}
    })


@app.route('/api/quick-create-client', methods=['POST'])
@login_required
def api_quick_create_client():
    """
    AJAX endpoint: create a Client on the fly from the order-creation form.
    Open to any logged-in user (not admin-only) - regular users place their
    own orders and need to be able to add a client for themselves, unlike
    the Detail/Product quick-create endpoints above which are catalog-
    management actions reserved for admins.
    """
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'Моля въведете име на клиента.'}), 400

    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        return jsonify({'status': 'error', 'message': eik_error}), 400

    client = Client(
        name=name,
        email=request.form.get('email', '').strip() or None,
        phone=request.form.get('phone', '').strip() or None,
        client_type='company' if request.form.get('client_type') == 'company' else 'individual',
        eik=eik,
        vat_number=request.form.get('vat_number', '').strip() or None,
        address=request.form.get('address', '').strip() or None,
        mol=request.form.get('mol', '').strip() or None,
    )
    db.session.add(client)
    db.session.commit()

    return jsonify({'status': 'success', 'client': {'id': client.id, 'name': client.name}})


@app.route('/api/quick-create-deliverer', methods=['POST'])
@login_required
def api_quick_create_deliverer():
    """AJAX endpoint: create a Deliverer on the fly from the order-creation form. See api_quick_create_client()."""
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'Моля въведете име на куриера.'}), 400

    eik, eik_error = _validate_eik(request.form.get('eik'))
    if eik_error:
        return jsonify({'status': 'error', 'message': eik_error}), 400

    deliverer = Deliverer(
        name=name,
        email=request.form.get('email', '').strip() or None,
        phone=request.form.get('phone', '').strip() or None,
        eik=eik,
        vat_number=request.form.get('vat_number', '').strip() or None,
        address=request.form.get('address', '').strip() or None,
        mol=request.form.get('mol', '').strip() or None,
    )
    db.session.add(deliverer)
    db.session.commit()

    return jsonify({'status': 'success', 'deliverer': {'id': deliverer.id, 'name': deliverer.name}})


@app.route('/admin/orders/<int:order_id>/assign_machine', methods=['POST'])
@login_required
def admin_assign_machine(order_id):
    """AJAX endpoint: assign or change the machine on an order."""
    if not current_user.is_admin and current_user.role != 'worker':
        return jsonify({'status': 'error', 'message': 'Нямате достъп.'}), 403

    order = Order.query.get_or_404(order_id)
    machine_id_raw = request.form.get('machine_id', '')

    if machine_id_raw and machine_id_raw.isdigit():
        machine = Machine.query.get(int(machine_id_raw))
        if not machine:
            return jsonify({'status': 'error', 'message': 'Невалидна машина.'}), 400
        order.machine_id = machine.id
        machine_name = machine.name
    else:
        order.machine_id = None
        machine_name = None

    db.session.commit()

    return jsonify({
        'status': 'success',
        'machine_name': machine_name
    })

# ============================================================
# ADD THESE THREE ROUTES TO app.py
# ============================================================
# Each corresponds to a new "Изтрий" (Delete) button added to the
# templates. Paste them near their related existing routes:
#   - delete_dxf_file      -> near get_geometry() / dashboard()
#   - delete_machine       -> near add_machine() / update_machine_status()
#   - admin_delete_material -> near admin_update_material() / admin_add_material()
#
# All three follow the same conventions already used elsewhere in
# app.py: POST-only, flash() feedback in Bulgarian, redirect back to
# the originating page, and a try/db.session.rollback() on failure.


@app.route('/dxf/<int:file_id>/delete', methods=['POST'])
@login_required
def delete_dxf_file(file_id):
    """
    Deletes one DXF upload from a user's personal library. Only the
    owning user or an admin may delete it - matches the same
    ownership check already used in get_geometry().
    """
    dxf_file = DxfFile.query.get_or_404(file_id)
    if dxf_file.user_id != current_user.id and not current_user.is_admin:
        flash('Нямате разрешение да изтриете този файл.', 'danger')
        return redirect(url_for('dashboard'))
    try:
        filename = dxf_file.filename
        db.session.delete(dxf_file)
        db.session.commit()
        flash(f'Файлът "{filename}" беше изтрит.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване: {str(e)}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/dxf/delete_all', methods=['POST'])
@login_required
def delete_all_dxf_files():
    """Deletes every DXF upload in the current user's own library."""
    try:
        deleted = DxfFile.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        flash(f'Изтрити бяха {deleted} файл(а) от библиотеката.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване: {str(e)}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/machines/<int:id>/delete', methods=['POST'])
@role_required('admin')
def delete_machine(id):
    """
    Deletes a machine. Orders and DxfFiles that reference it keep
    existing (machine_id is nullable on both), so they're detached
    rather than deleted - a removed machine shouldn't take historical
    orders/uploads down with it.
    """
    machine = Machine.query.get_or_404(id)
    try:
        Order.query.filter_by(machine_id=machine.id).update({'machine_id': None})
        DxfFile.query.filter_by(machine_id=machine.id).update({'machine_id': None})
        db.session.delete(machine)
        db.session.commit()
        flash(f'Машина "{machine.name}" беше изтрита.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване: {str(e)}', 'danger')
    return redirect(url_for('list_machines'))


@app.route('/admin/materials/<string:key>/delete', methods=['POST'])
@role_required('admin')
def admin_delete_material(key):
    """
    Deletes a material price entry. Blocked if any Detail still
    references it (Detail.material_key is a hard FK to
    MaterialPrice.key with no cascade) - deleting it out from under
    an existing catalog part would either crash on the FK constraint
    or silently orphan the part's pricing basis. Reassign/delete those
    Details first, then remove the material.
    """
    material = MaterialPrice.query.filter_by(key=key).first_or_404()
    in_use = Detail.query.filter_by(material_key=key).count()
    if in_use > 0:
        flash(
            f'Материалът "{material.display_name}" се използва от {in_use} детайл(а) '
            'и не може да бъде изтрит. Изтрийте или преместете тези детайли първо.',
            'danger'
        )
        return redirect(url_for('admin_materials'))
    try:
        db.session.delete(material)
        db.session.commit()
        flash(f'Материал "{material.display_name}" беше изтрит.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване: {str(e)}', 'danger')
    return redirect(url_for('admin_materials'))


if __name__ == '__main__':
    with app.app_context():
        # NOTE ON SCHEMA CHANGES: db.create_all() only creates tables that
        # don't exist yet - it will NOT add new columns to an existing
        # `order` / `order_item` table or backfill the new `order_item`
        # unit_price column, and it can't rewrite old status values ("Нова",
        # "В производство", "Завършена") into the new slugs ("new",
        # "in_production", "completed"). If you already have a database from
        # before this change, either drop the order/order_item tables (or the
        # whole DB, in dev) and let this recreate them, or run a manual
        # migration (Alembic, or hand-written ALTER TABLE + UPDATE
        # statements) before deploying this version.
        db.create_all()
        # No auto-created default admin account anymore - a hardcoded
        # admin/admin123 credential sitting in public source was a real risk.
        # To create the first admin on a brand-new database, run
        # python -m migration.change_admin_password (creates the user if it
        # doesn't exist yet).
        # Populate the MaterialPrice table with defaults on first run only -
        # existing rows (including any admin-edited prices) are never touched.
        seed_material_prices()
        # Same pattern for the services page's machine-park cards - only runs
        # if ServiceMachineCard is completely empty (see seed_service_machine_cards).
        seed_service_machine_cards()
        seed_index_machine_cards()
    # Off by default - debug mode exposes an interactive code-execution
    # debugger on unhandled exceptions, so it must be opted into explicitly.
    # Set FLASK_DEBUG=1 in your environment for local development.
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'

    # Auto-open the app in the browser on startup. When debug_mode is on,
    # Flask's reloader re-runs this entire script in a subprocess - without
    # this guard the browser would pop open twice. WERKZEUG_RUN_MAIN is only
    # set to 'true' inside that reloaded subprocess (the one actually
    # serving requests), so we only open there; when debug is off, there's
    # no reloader/subprocess at all, so we open immediately instead.
    if not debug_mode or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Timer(1.0, lambda: webbrowser.open('http://127.0.0.1:5000/')).start()

    app.run(debug=debug_mode)