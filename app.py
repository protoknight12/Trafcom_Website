from datetime import datetime, timedelta
import os
import json
import math
import re
import calendar
import difflib
import uuid
import io
import csv
import webbrowser
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from html import escape as html_escape
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_from_directory, send_file, g, session, has_request_context
from openpyxl import Workbook
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox
from ezdxf.math import bulge_to_arc
import random
import barcode
from barcode.writer import SVGWriter
import smtplib
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import pyotp
import qrcode
import qrcode.image.svg
import anthropic
import paho.mqtt.client as mqtt_client
from pymodbus.client import ModbusTcpClient
from flask_babel import Babel, gettext

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
# Default pool (size=5, max_overflow=10 -> 15 connections) proved too small
# under real concurrent load: a Solis Modbus read that stalls/retries on the
# shared, flaky WiFi-to-Modbus gateway (see SOLIS_INVERTER_MODBUS.md) holds
# its DB session's connection checked out for the whole stall, and enough of
# those piling up at once exhausted the pool - every OTHER page (not just
# Modbus ones) then failed with "QueuePool limit... connection timed out"
# too, since the pool is shared app-wide. Confirmed live 2026-09-06 (site
# froze under heavy Modbus polling, cleared by restarting - this raises the
# ceiling so it takes much more concurrent stalling to repeat).
# pool_pre_ping avoids handing out a connection Postgres has since dropped.
# SQLite (used by the test suite's in-memory DATABASE_URL) uses a StaticPool
# that doesn't accept pool_size/max_overflow, so only apply this to Postgres.
if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_size': 10, 'max_overflow': 20, 'pool_pre_ping': True}
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
# Belt-and-suspenders alongside CSRFProtect below: without an explicit
# SameSite, the session cookie's cross-site behavior is left to each
# browser's own default rather than a value this app controls.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Idle-timeout logout: a session cookie is valid for this long since the
# user's last request, not since login. Flask refreshes the cookie's
# expiry on every request by default (SESSION_REFRESH_EACH_REQUEST), so an
# active user never gets logged out mid-work - only a genuinely idle one.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=10)

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

# Visitor-facing multilingual support (public + client pages only - the
# staff/admin panel stays Bulgarian-only). Language choice lives in the
# session (no per-language URLs - see set_language()), so this never touches
# url_for() or route signatures anywhere in the app.
SUPPORTED_LANGUAGES = {'bg': 'Български', 'en': 'English', 'de': 'Deutsch'}
app.config['BABEL_DEFAULT_LOCALE'] = 'bg'


def get_locale():
    if not has_request_context():
        return 'bg'
    return session.get('language') if session.get('language') in SUPPORTED_LANGUAGES else 'bg'


babel = Babel(app, locale_selector=get_locale)


@app.context_processor
def inject_locale():
    return {'get_locale': get_locale, 'supported_languages': SUPPORTED_LANGUAGES}


def localized(obj, field):
    """Customer-facing translated field (Product/Service/MaterialPrice/Detail
    name/description) - falls back to the Bulgarian base field whenever the
    current locale is 'bg' or no translation was entered for that record."""
    locale = get_locale()
    if locale == 'bg':
        return getattr(obj, field)
    return getattr(obj, f'{field}_{locale}', None) or getattr(obj, field)


app.jinja_env.globals['localized'] = localized

# Unlike SECRET_KEY/DATABASE_URL, this is optional - the site works fine
# without it, only the chat widget degrades (see api_chat()). Some
# Console-issued keys are "identity-linked" and reject requests unless the
# target workspace is named explicitly - ANTHROPIC_WORKSPACE_ID is only
# needed for that key type (Console -> workspace settings URL has the id).
_anthropic_workspace_id = os.environ.get('ANTHROPIC_WORKSPACE_ID')
anthropic_client = anthropic.Anthropic(
    default_headers={'anthropic-workspace-id': _anthropic_workspace_id} if _anthropic_workspace_id else None,
) if os.environ.get('ANTHROPIC_API_KEY') else None

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['PRODUCT_IMAGES_FOLDER'] = os.path.join(app.static_folder, 'uploads', 'products')
os.makedirs(app.config['PRODUCT_IMAGES_FOLDER'], exist_ok=True)
# Same folder the seeded machine cards' images already live in (see
# SERVICE_MACHINE_CARDS_SEED/INDEX_MACHINE_CARDS_SEED) - admin-uploaded
# machine images join them here.
app.config['MACHINE_IMAGES_FOLDER'] = os.path.join(app.static_folder, 'img', 'machines')
os.makedirs(app.config['MACHINE_IMAGES_FOLDER'], exist_ok=True)
# Photos attached to OfferItem rows (see upload_offer_item_image()) - web-
# accessible like PRODUCT_IMAGES_FOLDER, since the editor shows a live
# thumbnail, and also read straight off disk when embedding into the
# exported .xlsx (see build_offer_workbook()).
app.config['OFFER_IMAGES_FOLDER'] = os.path.join(app.static_folder, 'uploads', 'offers')
os.makedirs(app.config['OFFER_IMAGES_FOLDER'], exist_ok=True)
# Reference photo of the real panel, shown as a scalable/movable backdrop
# under a panel's schematic (see ElectricalPanel.schematic_bg_filename /
# admin_panel_schematic.html) - just an underlay to trace over, never
# embedded anywhere else, so it lives alongside the other web-servable
# upload folders.
app.config['PANEL_BACKGROUND_FOLDER'] = os.path.join(app.static_folder, 'uploads', 'panel_backgrounds')
os.makedirs(app.config['PANEL_BACKGROUND_FOLDER'], exist_ok=True)
# Private (non-static) storage for Detail DXF files - unlike UPLOAD_FOLDER,
# files saved here are kept permanently, not deleted after processing. Never
# served via Flask's static route; only download_detail_dxf() (admin-only)
# reads from it.
app.config['DETAIL_DXF_FOLDER'] = os.path.join(os.getcwd(), 'detail_dxf_files')
os.makedirs(app.config['DETAIL_DXF_FOLDER'], exist_ok=True)
# Reference PDFs (drawings/specs) attached to a detail line while building an
# order - see OrderItemAttachment and the detail+operations picker on
# order_create.html. Same private/permanent storage convention as
# DETAIL_DXF_FOLDER; only download_order_item_file() (admin/staff-only) reads
# from it.
app.config['ORDER_ITEM_FILE_FOLDER'] = os.path.join(os.getcwd(), 'order_item_files')
os.makedirs(app.config['ORDER_ITEM_FILE_FOLDER'], exist_ok=True)
# ISO 9001 controlled documents (quality manual/procedures/work instructions/
# forms/records) and their revision history - see ControlledDocument/
# ControlledDocumentRevision. Same private/permanent storage convention as
# DETAIL_DXF_FOLDER.
app.config['CONTROLLED_DOCUMENT_FOLDER'] = os.path.join(os.getcwd(), 'controlled_documents')
os.makedirs(app.config['CONTROLLED_DOCUMENT_FOLDER'], exist_ok=True)
# Calibration certificates (ISO 9001 §7.1.5) - see InstrumentCalibrationRecord.
app.config['CALIBRATION_CERT_FOLDER'] = os.path.join(os.getcwd(), 'calibration_certificates')
os.makedirs(app.config['CALIBRATION_CERT_FOLDER'], exist_ok=True)
# Training certificates (ISO 9001 §7.2) - see TrainingRecord.
app.config['TRAINING_CERT_FOLDER'] = os.path.join(os.getcwd(), 'training_certificates')
os.makedirs(app.config['TRAINING_CERT_FOLDER'], exist_ok=True)


# ----------------- МОДЕЛИ В БАЗАТА ДАННИ -----------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    # roles: 'regular_user', 'worker', 'admin', 'web_designer', 'quality_control'
    role = db.Column(db.String(20), default='regular_user')
    # Nullable so pre-existing accounts don't need a backfill - required
    # going forward at /register since password reset depends on it.
    email = db.Column(db.String(150), unique=True, nullable=True)
    # Set by verify_email() once the user clicks the link sent by
    # _send_verification_email(). Not enforced anywhere (login/features work
    # regardless) - it's just a "did this address actually reach them" signal.
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    # Base32 TOTP secret (pyotp). Presence of a value is the on/off switch
    # for 2FA - see login()'s pending-2FA branch.
    totp_secret = db.Column(db.String(32), nullable=True)

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

    @property
    def can_manage_quality(self):
        """Admins and quality-control staff can log/view QC inspections."""
        return self.role in ('admin', 'quality_control')


class ActivityLog(db.Model):
    """
    Blanket audit trail, one row per successful state-changing request by a
    logged-in user - see the after_request hook near role_required() below.
    username/role are stored as plain snapshot strings (not a User FK) so a
    row still makes sense after the user is renamed/deleted, and reflects the
    role they had *at the time*, not whatever their role is now.
    """
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    username = db.Column(db.String(50), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(255), nullable=False)
    # Optional longer breakdown for actions where the one-line action summary
    # necessarily drops detail (e.g. every line of a delivery note or order,
    # not just a count) - see log_action()'s details= param. NULL when the
    # action text already says everything (e.g. a field-by-field diff).
    details = db.Column(db.Text, nullable=True)


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
    # Which billable Service(s) (hourly rate + machine type) priced the cut -
    # see calculate_cnc_price_multi_service(). Plain many-to-many, not a
    # single FK: the DXF calculator lets a job be priced against more than
    # one service at once (e.g. a combined cut+engrave pass), each billed at
    # its own rate for the same cut/pierce time - see upload.html's checkbox
    # picker. Can be empty for pre-refactor rows (priced under the old flat
    # cost_per_meter_cut/cost_per_pierce scheme); their calculated_price
    # stays a frozen historical value either way.
    services = db.relationship('Service', secondary='dxf_file_service', backref='dxf_files')


# See DxfFile.services above - one upload can be priced against several
# services at once, and one service obviously prices many uploads.
dxf_file_service = db.Table(
    'dxf_file_service',
    db.Column('dxf_file_id', db.Integer, db.ForeignKey('dxf_file.id'), primary_key=True),
    db.Column('service_id', db.Integer, db.ForeignKey('service.id'), primary_key=True),
)


class GeneratorPreset(db.Model):
    """
    A saved set of Panel Generator (templates/generator.html) slider/shape
    settings, so a user doesn't have to re-dial in a look from scratch.
    Owned by the user who saved it (one row per user+name, saving over an
    existing name overwrites it - see api_generator_presets_save()). Admins
    get a dashboard listing every user's presets and can copy one into their
    own account - see admin_generator_presets() / admin_generator_preset_copy().
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    settings_json = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user = db.relationship('User', backref='generator_presets')


class MaterialPrice(db.Model):
    """
    Per-material pricing, editable by admins at runtime instead of being
    hardcoded in source. `key` is the stable internal identifier used in
    DxfFile.material and the dashboard's material <select> - it's
    auto-generated when a material is created, not edited through the UI.

    cost_per_m2 is still a human-friendly EUR/m2 rate (see calculate_cnc_price()).
    cutting_speed_mm_per_min and pierce_rate_per_min replace the old flat
    cost_per_meter_cut/cost_per_pierce EUR rates - cutting/piercing are no
    longer priced directly off the material, they're priced off *time*
    (length/speed and pierce_count/rate minutes) at whichever Service's
    EUR/hour rate is selected for the job. Same material can be cut on a
    cheap or expensive machine; the material only determines how fast that
    machine can process it.
    """
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    # Optional customer-facing translations - see localized(). Empty/NULL
    # falls back to display_name (Bulgarian).
    display_name_en = db.Column(db.String(100), nullable=True)
    display_name_de = db.Column(db.String(100), nullable=True)
    cost_per_m2 = db.Column(db.Float, nullable=False)
    # Nullable, same reason as pierce_rate_per_min below: 'rods' and
    # 'profiles' stock is cut to length on a saw, not through the DXF-length/
    # cutting-speed pricing model, so neither carries a cutting speed.
    cutting_speed_mm_per_min = db.Column(db.Float, nullable=True)
    # Nullable: 'rods'/'profiles' stock is cut to length, never pierced, so
    # neither carries a pierce rate at all (see _parse_material_type callers).
    pierce_rate_per_min = db.Column(db.Float, nullable=True)
    # Standard stock sheet size this material entry represents (mm). Purely
    # informational catalog data - pricing still runs off the cut part's own
    # geometry (calculate_cnc_price), not off these. Optional/nullable since
    # older rows and non-sheet materials won't have them.
    sheet_length_mm = db.Column(db.Float, nullable=True)
    sheet_width_mm = db.Column(db.Float, nullable=True)
    thickness_mm = db.Column(db.Float, nullable=True)
    # 4th profile dimension - a profile's cross-section needs height AND
    # width (sheet_width_mm), unlike rods/pipes (diameter alone) or sheets
    # (no height at all), so the existing 3 generic dimension columns aren't
    # enough for 'profiles'. Only meaningful/shown for type == 'profiles'.
    height_mm = db.Column(db.Float, nullable=True)
    # Alternate weight-based pricing, informational/catalog-only - not read
    # by calculate_cnc_price()/_material_cost() (which stay on cost_per_m2).
    # Available for manual use (e.g. weight_kg * price_per_kg_*) when
    # building an Offer.
    price_per_kg_m2 = db.Column(db.Float, nullable=True)
    price_per_kg_m = db.Column(db.Float, nullable=True)
    # Actual measured weight (kg) of one stock unit (a full sheet, or one
    # linear meter for rods/pipes/profiles) - purely informational like the
    # two price_per_kg_* fields above, not read by the pricing engine.
    weight_kg = db.Column(db.Float, nullable=True)
    # Price of one whole stock unit (a full sheet, or one whole rod/pipe/
    # profile length) - informational/catalog-only like price_per_kg_m2/
    # price_per_kg_m above, not read by calculate_cnc_price()/_material_cost().
    # admin_materials.html uses it together with the dimension fields to
    # auto-fill cost_per_m2 (the field pricing actually runs off), but that's
    # a client-side convenience - cost_per_m2 stays independently editable.
    price_per_unit = db.Column(db.Float, nullable=True)
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
    # informational, shown alongside display_name where relevant. NOT part of
    # _find_or_create_delivery_target's matching anymore (see notes below) -
    # a supplier/manufacturer swap alone must reuse the same catalog row.
    brand = db.Column(db.String(100), nullable=True)
    # Free-text description carried over from a delivery-note line's
    # "Описание" field (DeliveryNoteItem.notes) when a brand-new material row
    # is created. Unlike brand, THIS one is part of
    # _find_or_create_delivery_target's matching - two lines with the same
    # name/dims/price but a different description are still a distinct batch
    # (per CLAUDE.md delivery-note task: boss revised the rule so brand no
    # longer splits a row, but description does).
    notes = db.Column(db.String(255), nullable=True)
    # Stock on hand, bumped by recording delivery notes (see DeliveryNoteItem
    # / admin_delivery_notes.html) - not editable by hand elsewhere.
    stock_quantity = db.Column(db.Float, nullable=False, default=0.0)
    # Reorder threshold, admin-set on admin_materials.html - purely a display
    # trigger (storage_materials.html turns the row's background yellow once
    # stock_quantity drops to/below this). Nullable: most materials don't
    # need one, and None means "no threshold set".
    min_quantity = db.Column(db.Float, nullable=True)


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

# Per-type display labels for the 3 generic dimension columns (sheet_length_mm,
# sheet_width_mm, thickness_mm - in that order), so a rod's "width" column
# reads "Диаметър" instead of "Ширина", etc. None hides that slot's meaning
# for the type (still stored/editable, just not a relevant physical
# dimension) - see material_dimension_labels() and admin_materials.html.
# ponytail: reuses the 3 existing sheet-named columns instead of adding
# dedicated diameter_mm/wall_thickness_mm columns - avoids a migration, at
# the cost of the column names not matching what they mean for non-sheet
# types. Add real columns if this ever needs to be less confusing at the DB
# level.
MATERIAL_DIMENSION_LABELS = {
    'sheets': ('Дължина на плочата (мм)', 'Ширина на плочата (мм)', 'Дебелина (мм)'),
    'rods': ('Дължина (мм)', 'Диаметър (мм)', None),
    'pipes': ('Дължина (мм)', 'Външен диаметър (мм)', 'Дебелина на стената (мм)'),
    'profiles': ('Дължина (мм)', 'Ширина (мм)', 'Дебелина на стената (мм)'),
    'other': (None, None, None),
}


def material_dimension_labels(type_key):
    """(length_label, width_label, thickness_label) for a material type, falling
    back to the sheet labels for unknown/blank types - same default as
    _parse_material_type()."""
    return MATERIAL_DIMENSION_LABELS.get(type_key, MATERIAL_DIMENSION_LABELS['sheets'])


def material_price_m2_label(type_key):
    """
    cost_per_m2 is only genuinely area-based for sheets (calculate_cnc_price()
    prices those off the DXF bounding-box area). Rods, pipes AND profiles are
    all bought/cut by the linear meter (a profile is a bar stock, same as a
    rod or pipe, just non-round - see _material_cost), not by a
    cross-section area - a pipe's "width" is its outer diameter, not a
    literal width (see DETAIL_DIMENSION_LABELS/MATERIAL_DIMENSION_LABELS), so
    diameter x length (or profile width x length) is not a real area.
    """
    if type_key == 'sheets':
        return 'Цена на м² плоча (€)'
    if type_key in ('rods', 'pipes', 'profiles'):
        return 'Цена на линеен метър (€)'
    return 'Цена на м² материал (€)'


def format_cut_dimensions(width, height, material_type):
    """
    Displays a cut part's DXF bounding-box width/height (see
    analyze_dxf_geometry/compute_bounding_box - always the same two numbers
    regardless of stock type) with vocabulary that matches the material's
    structural form: a rod/pipe's cross-section reads as a diameter, not a
    generic width. Used for Detail rows (admin_details.html) and personal
    DxfFile upload history (library.html) - both list mixed material types
    in one flat table, so this is a per-row label, not a column header.
    """
    if width is None or height is None:
        return '-'
    if material_type in ('rods', 'pipes'):
        return f"⌀{width:.2f} x {height:.2f} мм"
    return f"{width:.2f} x {height:.2f} мм"


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
        return None, gettext('ЕИК трябва да съдържа точно 9 цифри.')
    return value, None


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _validate_email(raw):
    """Blank is valid (email is optional on the model for pre-existing
    accounts) - callers that require it (e.g. /register) check for a blank
    value themselves. Returns (cleaned_value_or_None, error_message_or_None)."""
    value = (raw or '').strip().lower()
    if not value:
        return None, None
    if not _EMAIL_RE.fullmatch(value):
        return None, gettext('Невалиден имейл адрес.')
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
    # Optional customer-facing translations - see localized(). Empty/NULL
    # falls back to name (Bulgarian).
    name_en = db.Column(db.String(150), nullable=True)
    name_de = db.Column(db.String(150), nullable=True)
    material_key = db.Column(db.String(50), db.ForeignKey('material_price.key'), nullable=False)
    width = db.Column(db.Float, nullable=False)
    height = db.Column(db.Float, nullable=False)
    total_length = db.Column(db.Float, nullable=False)
    pierce_count = db.Column(db.Integer, nullable=False)
    calculated_price = db.Column(db.Float, nullable=False)
    geometry_json = db.Column(db.Text, nullable=True)
    # Which Service (hourly rate) priced this detail's cut - see
    # calculate_cnc_price(). Nullable for the same reason as DxfFile.service_id
    # (pre-refactor rows, and delivery-note-created bare-bones details that
    # skip DXF geometry entirely - see _find_or_create_delivery_target).
    cutting_service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=True)
    cutting_service = db.relationship('Service')
    # ERP code (shown as text + Code128 barcode) and internal part code (КД №)
    # printed on production labels - see print_label(). Optional/nullable
    # since older catalog parts won't have these set.
    erp_number = db.Column(db.Integer, nullable=True, unique=True)
    code_number = db.Column(db.String(100), nullable=True)
    # Stock on hand, bumped by recording delivery notes (see DeliveryNoteItem
    # / admin_delivery_notes.html) - not editable by hand elsewhere.
    stock_quantity = db.Column(db.Float, nullable=False, default=0.0)
    # Per-detail override of the material's thickness (mm) - editable from
    # detail_dxf_dashboard.html's "Материал и размери" tab. Deliberately its
    # own column rather than writing to MaterialPrice.thickness_mm: that
    # column is shared by every other detail/product using the same material
    # row, and editing it here must only affect this one detail. Falls back
    # to material.thickness_mm when unset (see detail_dxf_dashboard.html).
    thickness_mm = db.Column(db.Float, nullable=True)
    # Extra stock margin (mm) added on top of width/height before pricing -
    # e.g. a couple mm of scrap allowance left on each edge that isn't part
    # of the net cut geometry. Same width/height convention as the two base
    # columns (diameter/length for rods/pipes, width/height for sheets/
    # profiles - see DETAIL_DIMENSION_LABELS in detail_dxf_dashboard.html).
    # Kept separate from width/height rather than folded in, so the "Материал
    # и размери" tab can show the DXF-derived size and the manual margin as
    # two distinct numbers instead of one already-summed value. Nullable,
    # treated as 0 when unset - see effective_width/effective_height.
    extra_width_mm = db.Column(db.Float, nullable=True)
    extra_height_mm = db.Column(db.Float, nullable=True)

    material = db.relationship('MaterialPrice')

    @property
    def effective_width(self):
        return self.width + (self.extra_width_mm or 0)

    @property
    def effective_height(self):
        return self.height + (self.extra_height_mm or 0)

    @property
    def total_price(self):
        """calculated_price (the DXF cut) plus every extra Operation (milling,
        deburring, ...) attached to this detail - see Operation. What Product
        pricing (calculate_product_pricing) and the catalog listing actually
        charge for a detail, as opposed to calculated_price which is frozen
        to just the original cut."""
        return round(self.calculated_price + sum(op.cost for op in self.operations), 2)

    @property
    def material_price_breakdown(self):
        """Human-readable "quantity x rate" behind calculated_price, for
        display next to it (see detail_dxf_dashboard.html) - mirrors
        _material_cost()'s rods/pipes/profiles-vs-area branch exactly so this
        can never drift from the real calculation. Uses effective_width/
        effective_height (base size + extra margin), same as calculated_price
        itself."""
        if not self.material:
            return None
        width, height = self.effective_width, self.effective_height
        if self.material.type in ('rods', 'pipes', 'profiles'):
            return f"{height:.0f} мм × {self.material.cost_per_m2:.2f} €/м"
        area_m2 = (width * height) / 1_000_000
        return f"{area_m2:.3f} м² × {self.material.cost_per_m2:.2f} €/м²"


class DetailDxfFile(db.Model):
    """
    An original .dxf file kept for a Detail, shown on its admin-only file
    dashboard (see detail_dxf_dashboard()). Separate from Detail.geometry_json
    (the parsed shape data used for pricing/rendering) - process_dxf_upload()
    and the old admin_add_detail() flow used to delete the raw upload once
    geometry/price were extracted; this table is what keeps it around instead.
    Any logged-in user can upload a file here (e.g. a revision), but only
    admins may download - see download_detail_dxf().
    """
    id = db.Column(db.Integer, primary_key=True)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)  # on-disk name (uuid-prefixed, collision-safe)
    original_filename = db.Column(db.String(255), nullable=False)  # shown in the UI / used as download name
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    detail = db.relationship('Detail', backref=db.backref('dxf_files', cascade='all, delete-orphan', lazy=True))
    uploaded_by = db.relationship('User')


class Operation(db.Model):
    """
    An extra processing step attached to a Detail beyond its base laser cut
    (e.g. milling, deburring, welding) - see Detail.total_price. Unlike the
    cut itself, these have no DXF geometry to derive a duration from, so
    duration_minutes is entered directly by an admin rather than computed
    from length/pierce_count. `sequence` orders multiple operations on the
    same detail (e.g. mill before deburr).
    """
    id = db.Column(db.Integer, primary_key=True)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=False)
    sequence = db.Column(db.Integer, nullable=False, default=0)
    duration_minutes = db.Column(db.Float, nullable=False)
    # Cut length in mm, used instead of duration_minutes when the linked
    # Service.pricing_mode == 'length' (e.g. a length-based laser cutting
    # operation) - see cost. Unused (0) for time-based operations.
    length_mm = db.Column(db.Float, nullable=True)
    # Free-text note distinguishing operations that share a Service but mean
    # different things in practice (e.g. "лазерно рязане" in-house vs.
    # "външно лазерно рязане" outsourced) - optional, purely descriptive.
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    detail = db.relationship('Detail', backref=db.backref('operations', cascade='all, delete-orphan', lazy=True,
                                                            order_by='Operation.sequence'))
    service = db.relationship('Service')

    @property
    def cost(self):
        """duration_minutes priced at the linked Service's EUR/hour rate, or
        length_mm priced at its EUR/meter rate when the Service is length-based."""
        if self.service.pricing_mode == 'length':
            return round((self.length_mm or 0) / 1000.0 * (self.service.price_per_meter_eur or 0), 2)
        return round(self.duration_minutes * (self.service.price_per_hour_eur / 60.0), 2)


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
    # Optional customer-facing translations - see localized(). Empty/NULL
    # falls back to name/description (Bulgarian).
    name_en = db.Column(db.String(150), nullable=True)
    name_de = db.Column(db.String(150), nullable=True)
    description_en = db.Column(db.Text, nullable=True)
    description_de = db.Column(db.Text, nullable=True)
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
        # STATUS_LABELS values are looked up dynamically, so pybabel can't
        # statically extract them from here - their EN/DE translations are
        # added by hand in translations/*/LC_MESSAGES/messages.po.
        return gettext(STATUS_LABELS.get(self.status, self.status))

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


class OrderItemOperation(db.Model):
    """
    An ad-hoc processing step (cutting, bending, drilling...) picked at
    order-creation time for one standalone-detail OrderItem line - see
    order_create.html's per-detail operations picker, which mirrors the
    Detail catalog's own Operation/admin_details.html cart. Order-scoped
    rather than catalog-scoped: the same Detail can carry a different set of
    operations on different orders (e.g. a hinge that only needs drilling on
    one order but cutting+bending+drilling on another).

    Frozen like OrderItemComponent: its cost is folded into OrderItem's
    unit_price once, up front, in create_order() - this table only keeps the
    breakdown for display, it is never re-summed for pricing after that.
    """
    id = db.Column(db.Integer, primary_key=True)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_item.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=False)
    sequence = db.Column(db.Integer, nullable=False, default=0)
    duration_minutes = db.Column(db.Float, nullable=False)
    # Free-text note distinguishing operations that share a Service but mean
    # different things in practice (e.g. "лазерно рязане" in-house vs.
    # "външно лазерно рязане" outsourced) - same purpose as Operation.description.
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    order_item = db.relationship('OrderItem', backref=db.backref(
        'operations', cascade='all, delete-orphan', lazy=True, order_by='OrderItemOperation.sequence'))
    service = db.relationship('Service')

    @property
    def cost(self):
        return round(self.duration_minutes * (self.service.price_per_hour_eur / 60.0), 2)


class OrderItemAttachment(db.Model):
    """
    An optional reference PDF (drawing, spec sheet...) attached to one
    standalone-detail OrderItem line, picked in the same detail+operations
    section on order_create.html - see upload_order_item_pdf() (stages the
    file before the OrderItem exists) and create_order() (links it once the
    OrderItem is flushed). Admin/staff-only to download afterward, same rule
    as DetailDxfFile/download_detail_dxf().
    """
    id = db.Column(db.Integer, primary_key=True)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_item.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)  # on-disk name (uuid-prefixed, collision-safe)
    original_filename = db.Column(db.String(255), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    order_item = db.relationship('OrderItem', backref=db.backref(
        'attachments', cascade='all, delete-orphan', lazy=True))
    uploaded_by = db.relationship('User')


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
    details_subtotal = sum(pd.detail.total_price * pd.quantity for pd in product.product_details)
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


class ClientDeliveryNote(db.Model):
    """
    A goods-issued delivery note (стокова разписка към клиент) - the mirror
    of DeliveryNote: instead of restocking from a Supplier, it records stock
    leaving to a Client and decrements stock_quantity on each referenced
    MaterialPrice/Detail/Product line (via ClientDeliveryNoteItem, using
    _bump_stock with a negative quantity). See admin_client_delivery_notes.html.
    """
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    note_number = db.Column(db.String(100), nullable=True)
    note_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    client = db.relationship('Client')
    created_by = db.relationship('User')
    items = db.relationship('ClientDeliveryNoteItem', backref='client_delivery_note', cascade='all, delete-orphan', lazy=True)


class ClientDeliveryNoteItem(db.Model):
    """
    One issued line on a ClientDeliveryNote - same target_type/target_id
    convention as DeliveryNoteItem ('material'/'detail'/'product'), but
    always points at an existing catalog row: issuing stock that isn't
    already in the catalog makes no sense, unlike intake where a brand-new
    row can be created on arrival. description_snapshot freezes the name at
    issue time, same reasoning as DeliveryNoteItem.description_snapshot.
    """
    id = db.Column(db.Integer, primary_key=True)
    client_delivery_note_id = db.Column(db.Integer, db.ForeignKey('client_delivery_note.id'), nullable=False)
    target_type = db.Column(db.String(20), nullable=False)  # 'material' / 'detail' / 'product'
    target_id = db.Column(db.Integer, nullable=False)
    description_snapshot = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=True)
    notes = db.Column(db.String(255), nullable=True)


# Request status slugs, same ASCII-slug-as-CSS-class convention as
# STATUS_LABELS (Order.status) above.
REQUEST_STATUS_LABELS = {
    'new': 'Нова',
    'processing': 'В обработка',
    'ordered': 'Поръчана',
}


class MaterialRequest(db.Model):
    """
    A request to restock a material, raised from the storage materials page
    (Склад -> Материали) when stock is running low - separate from
    DeliveryNote, which records stock actually received. Tracks a request
    through new -> processing -> ordered; supplier_id (who it was ordered
    from) is only meaningful once status is 'ordered', reusing the same
    Supplier catalog as DeliveryNote.
    """
    id = db.Column(db.Integer, primary_key=True)
    material_id = db.Column(db.Integer, db.ForeignKey('material_price.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='new')
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    material = db.relationship('MaterialPrice')
    supplier = db.relationship('Supplier')
    created_by = db.relationship('User')

    @property
    def status_label(self):
        return REQUEST_STATUS_LABELS.get(self.status, self.status)


# Status slugs for ProductionOrder - same ASCII-slug-as-CSS-class convention
# as Order.status/STATUS_LABELS.
PRODUCTION_ORDER_STATUS_LABELS = {
    'pending': 'Чака изработка',
    'done': 'Завършена',
}


class ProductionOrder(db.Model):
    """
    A standalone "произведи N бр. от този детайл" job (admin_production_orders()),
    independent of any customer Order - for building up Detail.stock_quantity
    ahead of demand rather than fulfilling a specific sale. Reserves which
    MaterialPrice row (batch/lot) to draw from - the same physical material
    can exist as several catalog rows at different prices, one per delivery
    note batch (see CLAUDE.md's delivery-note "keep separate items separate"
    rule) - and freezes the calculated material need (planned_material_qty,
    same unit as MaterialPrice.stock_quantity: m² for sheets/other, linear
    meters for rods/pipes/profiles - see _detail_material_unit_qty()) at
    creation time, so a later edit to the Detail's dimensions never rewrites
    an already-planned job.

    Marking a job 'done' is the only thing that touches stock: it subtracts
    actual_material_qty (entered by whoever finished the job, to account for
    real-world waste vs. the planned figure - e.g. kerf/scrap) from the
    chosen material's stock_quantity, and adds `quantity` to the Detail's
    stock_quantity, both via the same _bump_stock() delivery notes use - see
    complete_production_order().

    Deleting a job (delete_production_order()) hard-deletes a 'pending' one
    outright - it never touched stock, nothing to keep a record of. A 'done'
    job is soft-deleted instead (reversed_at/reversed_by_id set, row kept):
    it already moved real stock, and admin_material_history() needs both the
    original "taken for production" movement AND the reversing "returned
    from production" movement to stay visible - hard-deleting the row would
    silently erase that either happened. reversed_at also doubles as "is
    this done job still active" - see admin_production_orders()'s
    pending_jobs/done_jobs queries, which exclude reversed rows.
    """
    id = db.Column(db.Integer, primary_key=True)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey('material_price.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    planned_material_qty = db.Column(db.Float, nullable=False)
    # Entered when marking the job done - what was actually drawn from
    # stock, same unit as planned_material_qty. Null while pending.
    actual_material_qty = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')  # 'pending' / 'done'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    completed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reversed_at = db.Column(db.DateTime, nullable=True)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    detail = db.relationship('Detail')
    material = db.relationship('MaterialPrice')
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    completed_by = db.relationship('User', foreign_keys=[completed_by_id])
    reversed_by = db.relationship('User', foreign_keys=[reversed_by_id])

    @property
    def status_label(self):
        return PRODUCTION_ORDER_STATUS_LABELS.get(self.status, self.status)

    @property
    def is_linear_material(self):
        return bool(self.material and self.material.type in ('rods', 'pipes', 'profiles'))

    @property
    def unit_label(self):
        return 'мм' if self.is_linear_material else 'м²'

    @property
    def planned_display(self):
        """planned_material_qty converted to the unit shown/entered in the UI -
        mm total length for bar stock (matches how Detail dimensions are
        already expressed everywhere else), m² for sheets."""
        return round(self.planned_material_qty * 1000, 1) if self.is_linear_material else round(self.planned_material_qty, 3)

    @property
    def actual_display(self):
        if self.actual_material_qty is None:
            return None
        return round(self.actual_material_qty * 1000, 1) if self.is_linear_material else round(self.actual_material_qty, 3)

    @property
    def material_available_display(self):
        """Currently available quantity of the linked material batch, in the
        same display unit as planned_display/actual_display - see
        _material_available_qty() (sheet stock is a sheet count, converted
        to an area here so it's directly comparable to planned/actual)."""
        qty = _material_available_qty(self.material)
        return round(qty * 1000, 1) if self.is_linear_material else round(qty, 3)


class Building(db.Model):
    """Top level of the factory map's location hierarchy (Сграда) - just a
    name; Rooms live inside it (see Room.building_id)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)

    rooms = db.relationship('Room', backref='building', lazy=True, order_by='Room.name')


class Room(db.Model):
    """One physical room/premises (Помещение) inside a Building - the unit
    the interactive factory map is actually drawn per (see
    admin_factory_map_room()): each room gets its own canvas, since
    Machine.pos_x/pos_y and ElectricalPanel.pos_x/pos_y are only meaningful
    relative to one room's layout, not the whole site at once."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    building_id = db.Column(db.Integer, db.ForeignKey('building.id'), nullable=False)

    panels = db.relationship('ElectricalPanel', backref='room', lazy=True, order_by='ElectricalPanel.name')


class ElectricalPanel(db.Model):
    """
    A physical electrical panel/board (Ел. табло) in a Room. The shop's
    Shelly/Modbus energy meters are mounted inside a panel (*Device.panel_id)
    rather than out on the machines themselves, and a Machine separately
    records which panel it's wired to (Machine.panel_id) - two different
    facts (where a meter physically sits vs. which machine's circuit it's
    clamped onto) that happen to both point at the same panel. Positioned on
    the room's map same as a Machine (pos_x/pos_y, percentage of that room's
    canvas).

    parent_panel_id is the actual electrical distribution link, independent
    of the Building/Room hierarchy - a main panel and the sub-panels it
    feeds are frequently in different rooms or even different buildings
    (e.g. a main incomer feeding a sub-panel in a separate hall), so this
    can freely cross both. A root panel (fed straight from the grid/meter,
    not from another panel here) simply has parent_panel_id = None.
    overview_pos_x/y position this panel on the site-wide overview map
    (admin_factory_map_overview()) - deliberately separate columns from
    pos_x/pos_y, since a panel's placement within its own room's floor plan
    and its placement in the abstract site-wide distribution diagram are two
    unrelated layouts; dragging one must never move the other.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    pos_x = db.Column(db.Float, nullable=True)
    pos_y = db.Column(db.Float, nullable=True)
    parent_panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    overview_pos_x = db.Column(db.Float, nullable=True)
    overview_pos_y = db.Column(db.Float, nullable=True)
    # Reference photo of the real panel, shown as a backdrop under this
    # panel's schematic (/admin/panels/<id>/schematic) to trace components
    # over - see PANEL_BACKGROUND_FOLDER/_save_upload(). scale is a
    # multiplier of the image's natural pixel size; pos_x/pos_y are the
    # image's own center, percent of the schematic canvas - same
    # translate(-50%,-50%) convention as every other draggable card in this
    # app, just applied to a photo instead of a symbol.
    schematic_bg_filename = db.Column(db.String(255), nullable=True)
    schematic_bg_scale = db.Column(db.Float, nullable=False, default=1.0)
    schematic_bg_pos_x = db.Column(db.Float, nullable=False, default=50.0)
    schematic_bg_pos_y = db.Column(db.Float, nullable=False, default=50.0)
    # How much the reference photo shows through under the drawn schematic -
    # explicit ask: "да има и прозрачност на подложката". 1.0 = fully
    # visible photo, 0.0 = invisible (pure schematic). Kept separate from
    # the drag lock (schemUnlock only guards position/scale) since fading
    # the photo in/out isn't a placement change.
    schematic_bg_opacity = db.Column(db.Float, nullable=False, default=0.85)

    child_panels = db.relationship('ElectricalPanel', backref=db.backref('parent_panel', remote_side=[id]))


PANEL_COMPONENT_TYPES = {
    'main_breaker': 'Главен прекъсвач',
    'breaker': 'Автоматичен предпазител',
    'fuse': 'Стопяем предпазител',
    'rcd': 'Дефектнотокова защита (RCD)',
    'contactor': 'Контактор',
    'busbar': 'Шина',
    'terminal': 'Клема',
    'meter': 'Електромер (означение)',
    # Researched before adding ("проучи ги...") - see componentIconSvg() in
    # admin_panel_schematic.html for each one's own icon/terminal layout:
    # - ethernet_switch: a plain network switch - N RJ45 ports in a row, all
    #   equivalent (no in/out direction), drawn as squares not circles.
    # - modbus_gateway: RS-485<->Ethernet converter - a Modbus terminal
    #   block (A/B/GND) plus one Ethernet port, single 'tap' row.
    # - schrack_urna: Schrack URNA0345 grid/system protection relay (used
    #   for PV/generator mains disconnect protection) - real terminal block
    #   transcribed from the manufacturer's own labeled diagram: A1/A2
    #   supply, N/L1/L2/L3 measuring input, 3 changeover relay outputs
    #   (11-12-14 / 21-22-24 / 31-32-34), 5 digital inputs each with its own
    #   common - see schrackUrnaIconSvg() in admin_panel_schematic.html for
    #   the fixed (not pole-count-scaled) two-row layout.
    # - smart_meter_3p: a 3-phase meter with real in/out pole pairs (like
    #   Trafcom's own DTSU666) PLUS an RS-485/Modbus port, unlike the plain
    #   'meter' type which has no comms tap.
    # - time_relay: a DIN-rail timer - its own supply (A1/A2) plus one or
    #   more changeover output contacts, drawn like a contactor with a
    #   clock face instead of a coil.
    'ethernet_switch': 'Ethernet суич',
    'modbus_gateway': 'Modbus към Ethernet',
    'schrack_urna': 'Schrack URNA 0345 (защита)',
    'smart_meter_3p': 'Смарт електромер (3-фазен)',
    'time_relay': 'Реле за време',
}


class PanelComponent(db.Model):
    """
    One symbol placed on the internal one-line schematic of an
    ElectricalPanel (see /admin/panels/<id>/schematic) - a breaker, fuse,
    contactor, busbar, terminal, etc. Purely a documentation/schematic
    layer: feeds_machine_id/feeds_panel_id/feeds_modbus_device_id let one
    optionally point at whatever real Machine/child ElectricalPanel/
    ModbusDevice this component's downstream side actually is (at most one
    of the three is expected to be set at a time - enforced in the route,
    not the schema, same as the rest of this app's optional single-choice
    FK groups e.g. ModbusDevice's grid_panel_id/main_panel_id/etc.), so the
    schematic can show "Предпазител 3 -> Лазер ECKERT" instead of a bare
    label. pos_x/pos_y are percent-of-canvas, same convention as Machine/
    ElectricalPanel's own map position, but scoped to this panel's own
    schematic canvas (a totally different coordinate space from the
    factory map).
    """
    id = db.Column(db.Integer, primary_key=True)
    panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=False)
    component_type = db.Column(db.String(20), nullable=False, default='breaker')
    name = db.Column(db.String(150), nullable=False)
    rated_current_a = db.Column(db.Float, nullable=True)
    poles = db.Column(db.Integer, nullable=True)
    manufacturer = db.Column(db.String(100), nullable=True)
    model = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    pos_x = db.Column(db.Float, nullable=True)
    pos_y = db.Column(db.Float, nullable=True)
    # Per-element icon size multiplier (independent of pos_x/pos_y and of
    # every other component's own scale) - see admin_update_panel_component_scale()/
    # the +/- buttons on each card in admin_panel_schematic.html.
    scale = db.Column(db.Float, nullable=False, default=1.0)
    feeds_machine_id = db.Column(db.Integer, db.ForeignKey('machine.id'), nullable=True)
    feeds_panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    feeds_modbus_device_id = db.Column(db.Integer, db.ForeignKey('modbus_device.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    panel = db.relationship('ElectricalPanel', foreign_keys=[panel_id], backref=db.backref('components', cascade='all, delete-orphan'))
    feeds_machine = db.relationship('Machine')
    feeds_panel = db.relationship('ElectricalPanel', foreign_keys=[feeds_panel_id])
    feeds_modbus_device = db.relationship('ModbusDevice')

    @property
    def feeds_label(self):
        if self.feeds_machine:
            return f'Машина: {self.feeds_machine.name}'
        if self.feeds_panel:
            return f'Табло: {self.feeds_panel.name}'
        if self.feeds_modbus_device:
            return f'Устройство: {self.feeds_modbus_device.name}'
        return None


# Despite the name (kept for the existing 'phase_type' column/routes - a
# plain unconstrained VARCHAR(20), so new values need no migration), this
# now covers non-power connection kinds too - explicit ask: "трябва да имам
# още видове връзки: modbus, ethernet, current transformer" - a signal/data
# cable or a CT loop drawn on the same schematic, visually distinct from an
# actual power conductor (see the .pwire-modbus/-ethernet/-ct CSS rules and
# PHASE_RANK's three_phase-only bundling in admin_panel_schematic.html).
# All of them behave like 'single_phase' structurally (one plain wire, any
# pole/side, no 3-pole requirement) - only 'three_phase' has special rules.
PANEL_WIRE_PHASE_TYPES = {
    'three_phase': 'Трифазна', 'single_phase': 'Монофазна',
    'modbus': 'Modbus', 'ethernet': 'Ethernet', 'ct': 'Токов трансформатор (CT)',
}

# A wire can only connect two components whose ports actually match its own
# kind - explicit ask: "връзки може да се създават само от един вид ETH-ETH
# MODBUS-MODBUS и така нататък" (no Ethernet cable landing on a breaker's
# power pole, no Modbus wire ending at a plain fuse, etc.). 'busbar'/
# 'terminal' are generic pass-through points (a DIN rail / a screw terminal
# block carries whatever real cable is landed on it) so they're allowed for
# every kind. 'ethernet_switch' has only network ports; 'modbus_gateway'
# bridges Modbus<->Ethernet so it allows both; 'smart_meter_3p' adds an
# RS-485/Modbus tap to its own power poles. 'schrack_urna' is single_phase
# ONLY - its real terminal diagram (A1/A2 supply, N/L1/L2/L3 measuring
# input, 3 dry-contact relay outputs, 5 digital inputs) has no genuine
# 3-pole power pass-through, no CT input and no comms port at all, so a
# three_phase bundle (which always wires pole1-2-3 to pole1-2-3) would
# land on the wrong physical terminals - each of its 24 terminals is only
# ever wired individually.
PANEL_WIRE_TYPE_COMPONENT_TYPES = {
    'three_phase': {'main_breaker', 'breaker', 'fuse', 'rcd', 'contactor', 'busbar', 'terminal', 'meter', 'smart_meter_3p', 'time_relay'},
    'single_phase': {'main_breaker', 'breaker', 'fuse', 'rcd', 'contactor', 'busbar', 'terminal', 'meter', 'smart_meter_3p', 'time_relay', 'schrack_urna'},
    'modbus': {'modbus_gateway', 'smart_meter_3p', 'busbar', 'terminal'},
    'ethernet': {'ethernet_switch', 'modbus_gateway', 'busbar', 'terminal'},
    'ct': {'meter', 'smart_meter_3p', 'busbar', 'terminal'},
}


def _panel_wire_type_error(phase_type, *components):
    """Returns a Bulgarian error string if any given component's type isn't
    valid for this wire kind, else None - shared by admin_add_panel_wire()
    and admin_retarget_panel_wire()."""
    allowed = PANEL_WIRE_TYPE_COMPONENT_TYPES.get(phase_type)
    if not allowed:
        return None
    for c in components:
        if c.component_type not in allowed:
            return (f'"{PANEL_WIRE_PHASE_TYPES.get(phase_type, phase_type)}" връзка не може да свързва '
                    f'елемент от тип "{PANEL_COMPONENT_TYPES.get(c.component_type, c.component_type)}".')
    return None


class PanelWire(db.Model):
    """One drawn line between one specific numbered pole/terminal of a
    PanelComponent and one on another (or the same component's) symbol on
    the same panel's schematic - purely visual/topological, no live data of
    its own. from_side/to_side is 'in' (line-side, top row of terminal
    circles) or 'out' (load-side, bottom row) for a normal component, or
    'tap' for a busbar (single row of connection points, no in/out
    distinction - see componentIconSvg() in admin_panel_schematic.html).
    phase_type is purely a documentation label (three-phase vs single-phase
    run) - each wire is still exactly one drawn conductor between two
    specific terminals, not an auto-bundle of poles."""
    id = db.Column(db.Integer, primary_key=True)
    panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=False)
    from_component_id = db.Column(db.Integer, db.ForeignKey('panel_component.id'), nullable=False)
    to_component_id = db.Column(db.Integer, db.ForeignKey('panel_component.id'), nullable=False)
    from_pole = db.Column(db.Integer, nullable=False, default=1)
    from_side = db.Column(db.String(3), nullable=False, default='out')
    to_pole = db.Column(db.Integer, nullable=False, default=1)
    to_side = db.Column(db.String(3), nullable=False, default='in')
    phase_type = db.Column(db.String(20), nullable=False, default='three_phase')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    panel = db.relationship('ElectricalPanel', backref=db.backref('wires', cascade='all, delete-orphan'))
    from_component = db.relationship('PanelComponent', foreign_keys=[from_component_id], backref='wires_from')
    to_component = db.relationship('PanelComponent', foreign_keys=[to_component_id], backref='wires_to')


class Machine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), default='idle')  # idle, running, maintenance
    last_maintenance = db.Column(db.DateTime, default=datetime.utcnow)
    # Free-text category (e.g. 'laser', 'mill_3axis', 'plasma') matched against
    # Service.machine_type - see Service. Informational grouping only, not a
    # separate lookup table (same reasoning as MaterialPrice.type): a small,
    # shop-specific, open-ended set that admins type rather than pick from a
    # fixed enum. Nullable since existing machines predate this field.
    machine_type = db.Column(db.String(50), nullable=True)
    # Which Room this machine physically stands in - see Room's docstring.
    # Nullable: a machine not yet placed anywhere just doesn't show on any
    # room's map until assigned (see edit_machine_window()).
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=True)
    # Which ElectricalPanel this machine's power is wired to - independent of
    # room_id (usually the same room, but not assumed) and independent of
    # shelly_devices below (that's which meter *reads* this machine; this is
    # which panel its circuit actually terminates at).
    panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    # Position on its room's map (/admin/factory-map/room/<id>), as a
    # percentage (0-100) of that room's canvas width/height - resolution-
    # independent so the same saved position still lands correctly however
    # big the browser window is. Nullable: a machine with no position yet
    # falls back to an auto-arranged grid slot in the template rather than
    # stacking every un-placed machine at (0, 0) - see admin_factory_map_room().
    pos_x = db.Column(db.Float, nullable=True)
    pos_y = db.Column(db.Float, nullable=True)

    room = db.relationship('Room', backref='machines')
    panel = db.relationship('ElectricalPanel', foreign_keys=[panel_id], backref='wired_machines')


class Service(db.Model):
    """
    A billable operation type (e.g. "Лазерно рязане", "Фрезоване - 3 оси")
    admins manage on /admin/services - see admin_services(). Carries the
    EUR/hour rate the new time-based pricing engine (calculate_cnc_price())
    uses for the base DXF cut, and/or the rate an Operation bills its
    duration_minutes at for extra post-processing steps on a Detail.
    machine_type is a free-text category (matched against Machine.machine_type)
    - kept as a quick display label/grouping hint even now that `machines`
    below gives the real, specific link.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    machine_type = db.Column(db.String(50), nullable=True)
    price_per_hour_eur = db.Column(db.Float, nullable=False)
    # 'time' (default) prices an attached Operation by duration_minutes *
    # price_per_hour_eur; 'length' prices it by length_mm * price_per_meter_eur
    # instead - e.g. a laser-cutting service billed by cut length rather than
    # machine time (see Operation.cost). Only affects the extra-operations
    # picker - the base DXF cut (calculate_cnc_price) always uses the hourly
    # rate regardless of this flag, so it never changes how a Detail's own
    # cut is priced.
    pricing_mode = db.Column(db.String(10), nullable=False, default='time')
    # EUR per meter (1000 mm) of cut length - only used when pricing_mode == 'length'.
    price_per_meter_eur = db.Column(db.Float, nullable=True)
    description = db.Column(db.Text, nullable=True)
    # Optional customer-facing translations - see localized(). Empty/NULL
    # falls back to name/description (Bulgarian).
    name_en = db.Column(db.String(150), nullable=True)
    name_de = db.Column(db.String(150), nullable=True)
    description_en = db.Column(db.Text, nullable=True)
    description_de = db.Column(db.Text, nullable=True)
    # Whether the public /services page shows this service's hourly rate -
    # see services.html's "ЦЕНИ НА УСЛУГИ" section. Toggled per-service from
    # /admin/services; some services stay listed there without a public price.
    show_price = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    machines = db.relationship('Machine', secondary='service_machine', backref='services')


#  Which physical Machine(s) can actually perform a given Service - plain
#  many-to-many for the same reason as shelly_device_machines below: a
#  service (e.g. "Фрезоване - 3 оси") can run on more than one machine of
#  that type, and one machine can offer more than one service (e.g. a
#  multi-axis lathe doing both turning and milling operations).
service_machine = db.Table(
    'service_machine',
    db.Column('service_id', db.Integer, db.ForeignKey('service.id'), primary_key=True),
    db.Column('machine_id', db.Integer, db.ForeignKey('machine.id'), primary_key=True),
)


#  One meter can legitimately feed more than one machine (a shared sub-panel
#  or bus - exactly the kind of ambiguous shared feed the phase-C standing
#  load investigation turned up, see the Shelly section further down), and
#  one machine could in principle have more than one meter on it (a main
#  drive meter plus a separate auxiliary meter). Plain many-to-many, not a FK
#  column on either side.
shelly_device_machines = db.Table(
    'shelly_device_machine',
    db.Column('shelly_device_id', db.Integer, db.ForeignKey('shelly_device.id'), primary_key=True),
    db.Column('machine_id', db.Integer, db.ForeignKey('machine.id'), primary_key=True),
)


CONNECTION_TYPES = {
    'ip': 'IP (HTTP)',
    'mqtt': 'MQTT',
    'udp_rpc': 'RPC през UDP',
    'coiot': 'CoIoT',
}

MODBUS_DEVICE_TYPES = {
    'dtsu666': 'DTSU666 електромер',
    'solis_s6': 'Solis S6 хибриден инвертор',
    'solis_grid_meter': 'Виртуален "Мрежа" измервател (през друг инвертор)',
}

TEMP_SENSOR_TYPES = {
    'shelly_ht_gen1': 'Shelly H&T (Gen1)',
    'shelly_gen2': 'Shelly Plus/Pro H&T (Gen2+)',
    'generic_flat': 'Общ формат (prefix/temperature, prefix/humidity)',
}

CONVECTOR_TYPES = {
    'shelly_gen1': 'Shelly (Gen1) - /relay/N',
    'shelly_gen2': 'Shelly Plus/Pro (Gen2+) - Switch.*',
}

# Only 'ip'/'mqtt' of CONNECTION_TYPES are implemented for Convector (see
# _shelly_convector_status()/_shelly_convector_set()) - filtered down
# rather than a separate dict so the label text stays one source of truth
# with ShellyDevice's own connection-type selector.
CONVECTOR_CONNECTION_TYPES = {k: v for k, v in CONNECTION_TYPES.items() if k in ('ip', 'mqtt')}

# Vehicle.deadlines/vehicle_deadline_status() warning window - see
# inject_vehicle_alerts(). A deadline starts showing as 'warning' this many
# days before it expires, and naturally keeps showing (as 'expired') every
# day after, since status is recomputed fresh on every request rather than
# scheduled - no cron/email involved for v1.
VEHICLE_WARNING_DAYS = 15


def vehicle_deadline_status(expiry_date):
    """('ok'|'warning'|'expired'|'unset', days_remaining) for one Vehicle
    deadline date. days_remaining is negative once expired, None only for
    'unset' (no date on file)."""
    if not expiry_date:
        return 'unset', None
    days = (expiry_date - datetime.utcnow().date()).days
    if days < 0:
        return 'expired', days
    if days <= VEHICLE_WARNING_DAYS:
        return 'warning', days
    return 'ok', days


def _add_months(d, months):
    """d shifted by whole months (can be negative), clamping the day into
    the target month (e.g. 31 Jan - 1 month -> 28/29 Feb) - used by
    Vehicle.insurance_installment_dates to derive the 4 quarterly ГО due
    dates purely from insurance_expiry (assumes a 12-month policy)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day).date()


class ShellyDevice(db.Model):
    """
    A Shelly energy meter feeding the live power dashboard (/admin/power) -
    see the SHELLY ENERGY MONITORING section near the bottom of this file.
    Added/removed directly from that page (admin_power_add_device() /
    admin_power_delete_device()) and takes effect on the very next poll, no
    app restart - this replaced the original SHELLY_DEVICES env var as the
    source of truth once machines needed to be manageable from the UI rather
    than by editing a config file (see seed_shelly_devices() for the one-time
    migration of whatever was in that env var). host is unique so the same
    meter can't end up polled twice under two different labels.

    `machines` optionally ties a meter to any number of real Machine rows
    (the shop's CNC/laser catalog, via the shelly_device_machines table
    above) so the power dashboard can show which machine(s) a reading
    belongs to, and each one's own status. Deliberately optional and
    separate from `name`: a meter can be added and monitored before anyone
    has confirmed which physical machine(s) its clamps are actually on (see
    the panel-inspection note in the Shelly section) - the free-text label
    always works, machine links are filled in once that's known. No cascade
    config needed beyond the plain secondary table: SQLAlchemy removes the
    matching shelly_device_machine rows itself when either side is deleted,
    same effect delete_machine() already gets explicitly for Order/DxfFile.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    # Nullable: a device monitored purely over MQTT (mqtt_topic set) may have
    # no known/reachable IP to poll - admin_power_add_device() requires at
    # least one of host/mqtt_topic, not both.
    host = db.Column(db.String(100), nullable=True, unique=True)
    # Which ElectricalPanel this meter is physically mounted inside - see
    # ElectricalPanel's docstring. Nullable: a meter can be added and
    # monitored before its panel is documented, same as `machines` below.
    panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    # MQTT topic prefix this device publishes under (e.g. "shellies/thermopump_em3"
    # for a Gen1 device with the default topic root, or a bare custom prefix
    # like "hale1_conv_gol1" if it was reconfigured) - see start_mqtt_listener()/
    # shelly_device_snapshot().
    mqtt_topic = db.Column(db.String(150), nullable=True)
    # Explicit choice of which live-data transport shelly_device_snapshot()
    # uses - see CONNECTION_TYPES. Deliberately explicit rather than only
    # inferred from "mqtt_topic set vs not": 'udp_rpc'/'coiot' are real
    # Shelly transports (Gen2 outbound RPC over UDP; Gen1's CoAP-based CoIoT)
    # that aren't implemented yet - confirmed unreachable from this dev host
    # even with CoIoT's unicast peer pointed straight at it (see
    # shelly_device_snapshot()'s comment), most likely inbound UDP being
    # firewalled on this machine rather than anything wrong on the device
    # side. Kept as selectable now so the UI/data model don't need to change
    # again once a working listener exists; until then both simply report
    # "not implemented" rather than silently falling back to host/mqtt_topic.
    connection_type = db.Column(db.String(20), nullable=False, default='ip')
    machines = db.relationship('Machine', secondary=shelly_device_machines, backref='shelly_devices')
    panel = db.relationship('ElectricalPanel', backref='shelly_devices')


modbus_device_machines = db.Table(
    'modbus_device_machine',
    db.Column('modbus_device_id', db.Integer, db.ForeignKey('modbus_device.id'), primary_key=True),
    db.Column('machine_id', db.Integer, db.ForeignKey('machine.id'), primary_key=True),
)


class ModbusDevice(db.Model):
    """
    A Modbus TCP energy meter (e.g. the shop's DTSU666) - separate from
    ShellyDevice since Modbus meters aren't Shelly hardware at all and have
    no HTTP/MQTT API, just raw holding/input registers whose meaning is
    entirely manufacturer/firmware-specific. Register decoding for a given
    model (once confirmed against a live unit - see
    admin_modbus_read_registers()) lives in a dedicated snapshot function
    (e.g. _dtsu666_snapshot()) keyed off of this row's connection details,
    not a column here - there's currently no per-device "which model" field
    since only DTSU666 is wired up.

    panel_id/machines mirror ShellyDevice's fields exactly (same
    "meter physically sits in a panel" / "meter reads these machines"
    distinction - see ElectricalPanel's docstring) so a Modbus meter
    participates in the factory map's per-panel power aggregation and
    per-machine live readings the same way a Shelly one does.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    host = db.Column(db.String(100), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=502)
    unit_id = db.Column(db.Integer, nullable=False, default=1)
    # Which register map/snapshot function to decode this device with (see
    # _dtsu666_snapshot()/_solis_snapshot()) - 'dtsu666' is the original,
    # sole model this table supported, kept as the default so existing rows
    # need no backfill.
    device_type = db.Column(db.String(20), nullable=False, default='dtsu666')
    notes = db.Column(db.Text, nullable=True)
    panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    # Only meaningful for device_type='solis_s6' - which physical PV module
    # spec sheet (SolarPanelModel) this inverter's whole side of the roof is
    # wired with. One inverter = one uniform module model in this shop, so
    # this lives here rather than per-SolarPanel row.
    panel_model_id = db.Column(db.Integer, db.ForeignKey('solar_panel_model.id'), nullable=True)
    # Only meaningful for device_type='solis_s6' - which ElectricalPanel each
    # of the inverter's 4 AC "ports" actually feeds/connects to (see
    # _solis_snapshot()'s 'meter'/'load'/'generator' blocks for the live
    # readings themselves - this is purely "where does the wire go",
    # independent of `panel` above, which is where the inverter's OWN meter
    # is physically mounted). All nullable/independent of each other - a
    # port not wired to anything tracked here just has no line drawn for it
    # on admin_factory_map_overview.html.
    grid_panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    main_panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    backup_panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    generator_panel_id = db.Column(db.Integer, db.ForeignKey('electrical_panel.id'), nullable=True)
    # Only meaningful for device_type='solis_grid_meter' - a *virtual* row
    # with no Modbus connection of its own, representing a physical smart
    # meter that's wired into another Solis inverter's own Modbus network
    # (e.g. the grid CT in the main distribution panel, relayed through that
    # inverter's own 'meter' block - see _solis_snapshot()) rather than
    # polled directly. Lets that meter show up as its own separate card in
    # Consumption/the factory map (its own name, its own panel_id) without
    # a second, redundant Modbus read of the same registers - see
    # _solis_grid_meter_view_snapshot(), which derives this row's snapshot
    # from source_device's own already-fetched one.
    source_device_id = db.Column(db.Integer, db.ForeignKey('modbus_device.id'), nullable=True)

    machines = db.relationship('Machine', secondary=modbus_device_machines, backref='modbus_devices')
    panel = db.relationship('ElectricalPanel', foreign_keys=[panel_id], backref='modbus_devices')
    panel_model = db.relationship('SolarPanelModel')
    grid_panel = db.relationship('ElectricalPanel', foreign_keys=[grid_panel_id])
    main_panel = db.relationship('ElectricalPanel', foreign_keys=[main_panel_id])
    backup_panel = db.relationship('ElectricalPanel', foreign_keys=[backup_panel_id])
    generator_panel = db.relationship('ElectricalPanel', foreign_keys=[generator_panel_id])
    source_device = db.relationship('ModbusDevice', remote_side=[id])


BATTERY_STACK_BMS_PORTS = {'1': 'БМС порт 1', '2': 'БМС порт 2'}
# See BatteryStack.source_type's docstring - 'modbus' has no register map
# behind it yet, kept here only so the dropdown can offer/save the choice.
BATTERY_STACK_SOURCE_TYPES = {'inverter': 'През инвертор (БМС порт)', 'modbus': 'Отделен Modbus BMS (все още не е поддържано)'}


class Cabinet(db.Model):
    """
    Шкаф - a physical enclosure that can hold one or more BatteryStacks (a
    big cabinet sometimes houses both BMS ports' stacks side by side). Just
    a name/notes container - the fields the user actually cares about
    (inverter/BMS link, brand/model/serial, min/max battery count, room)
    live on BatteryStack itself, one level down.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class BatteryStack(db.Model):
    """
    STACK - a group of series-connected battery modules (e.g. 11x Dyness
    Stack100) behind one BMS port of one Solis inverter - see
    _solis_snapshot()'s 'battery_groups' in this file (index 0 -> bms_port
    '1', index 1 -> bms_port '2'). Lives inside a Cabinet. inverter_device_id/
    bms_port are both nullable - a stack can be catalogued (batteries counted,
    room assigned) before it's actually wired up and confirmed against a
    live BMS port reading.
    """
    id = db.Column(db.Integer, primary_key=True)
    cabinet_id = db.Column(db.Integer, db.ForeignKey('cabinet.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    # How this stack's live SOC/voltage/temperature is read - 'inverter'
    # (the only working option right now) means "through inverter_device_id's
    # own BMS-port register block" (see _solis_read_extra_blocks()'s
    # battery_groups, reused from that inverter's already-fetched snapshot -
    # see _battery_stack_live_data()). 'modbus' is reserved for a future
    # stack with its own independent Modbus BMS connection - no register map
    # exists for one yet, so it currently just shows as unavailable.
    source_type = db.Column(db.String(20), nullable=False, default='inverter')
    inverter_device_id = db.Column(db.Integer, db.ForeignKey('modbus_device.id'), nullable=True)
    bms_port = db.Column(db.String(1), nullable=True)  # '1' or '2' - see BATTERY_STACK_BMS_PORTS
    brand = db.Column(db.String(100), nullable=True)
    model = db.Column(db.String(100), nullable=True)
    serial_number = db.Column(db.String(100), nullable=True)
    min_batteries = db.Column(db.Integer, nullable=True)
    max_batteries = db.Column(db.Integer, nullable=True)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=True)
    # Position on its room's map (admin_factory_map_room.html), percentage of
    # canvas width/height - same convention as Machine.pos_x/pos_y.
    pos_x = db.Column(db.Float, nullable=True)
    pos_y = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cabinet = db.relationship('Cabinet', backref=db.backref('stacks', cascade='all, delete-orphan'))
    inverter = db.relationship('ModbusDevice', backref='battery_stacks')
    room = db.relationship('Room', backref='battery_stacks')

    @property
    def battery_count(self):
        return len(self.batteries)

    @property
    def place_label(self):
        if self.room:
            return f'{self.room.building.name} / {self.room.name}'
        return '—'

    @property
    def total_energy_kwh(self):
        """Sum of each battery's energy content - from its BatteryModel's
        energy_kwh when one is assigned, falling back to voltage*capacity_ah
        for older/manually-entered rows with no model. None if no battery
        in the stack has enough info to compute a figure."""
        total = 0.0
        have_any = False
        for b in self.batteries:
            if b.model and b.model.energy_kwh:
                kwh = b.model.energy_kwh
            elif b.voltage and b.capacity_ah:
                kwh = b.voltage * b.capacity_ah / 1000.0
            else:
                kwh = None
            if kwh:
                total += kwh
                have_any = True
        return round(total, 2) if have_any else None


class BatteryModel(db.Model):
    """
    Reference spec sheet for a battery module product, so the "add battery"
    form can pick a model from a dropdown instead of retyping voltage/
    capacity by hand for every unit - see Battery.model_id. Dyness S51100
    (this shop's actual hardware) sourced from multiple independent
    distributor listings (KaMeaSolar, Rebor, CCL Solar, ONSA Plus, Jardis,
    LirikSolar) since Dyness's own datasheet PDF wasn't directly fetchable -
    core figures (51.2V/100Ah/5.12kWh, 100A continuous, 657x460x292mm,
    45kg) agreed everywhere checked; cycle_life (one source said 6000, the
    rest incl. the manufacturer's own site said >=8000) and
    protection_rating (IP65 vs IP66) each had one outlier source - kept
    here as the majority/manufacturer figure with the conflict noted rather
    than silently picking one.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False, unique=True)
    manufacturer = db.Column(db.String(80), nullable=False, default='Dyness')
    chemistry = db.Column(db.String(40), nullable=True)
    nominal_voltage_v = db.Column(db.Float, nullable=False)
    capacity_ah = db.Column(db.Float, nullable=False)
    energy_kwh = db.Column(db.Float, nullable=True)
    usable_energy_kwh = db.Column(db.Float, nullable=True)
    continuous_current_a = db.Column(db.Float, nullable=True)
    max_discharge_power_kw = db.Column(db.Float, nullable=True)
    round_trip_efficiency_pct = db.Column(db.Float, nullable=True)
    cycle_life = db.Column(db.Integer, nullable=True)
    length_mm = db.Column(db.Float, nullable=True)
    width_mm = db.Column(db.Float, nullable=True)
    height_mm = db.Column(db.Float, nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
    protection_rating = db.Column(db.String(20), nullable=True)
    communication = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class Battery(db.Model):
    """One physical battery module within a BatteryStack."""
    id = db.Column(db.Integer, primary_key=True)
    stack_id = db.Column(db.Integer, db.ForeignKey('battery_stack.id'), nullable=False)
    model_id = db.Column(db.Integer, db.ForeignKey('battery_model.id'), nullable=True)
    voltage = db.Column(db.Float, nullable=True)
    capacity_ah = db.Column(db.Float, nullable=True)
    serial_number = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    stack = db.relationship('BatteryStack', backref=db.backref('batteries', cascade='all, delete-orphan'))
    model = db.relationship('BatteryModel')


class SolarPanelModel(db.Model):
    """
    Reference spec sheet for the physical PV module product used on this
    roof - Tongwei/TW Solar TWMND-72HD575 (inverter 1's side) and
    TWMND-72HD595 (inverter 2's side). Both share the exact same
    2278x1134x30mm frame/glass size (just a different power bin of the same
    cell line), cross-confirmed across independent distributor pages
    (Synapsun, Liriksolar, czpowersourcing) since the manufacturer's own PDF
    datasheet (tongwei.cn) refused direct access (401). Electrical figures
    below (Voc/Isc/Vmp/Imp/efficiency) are the Synapsun comparison table -
    two other distributor pages for the 575W variant quoted slightly
    different Voc/Isc (normal for a binned product, no two exact sources
    agreed) so treat these as representative/typical, not a guaranteed
    per-unit figure - unlike dimensions/weight/cell count, which agreed
    everywhere checked.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False, unique=True)
    manufacturer = db.Column(db.String(80), nullable=False, default='Tongwei Solar (TW Solar)')
    rated_power_w = db.Column(db.Float, nullable=False)
    length_mm = db.Column(db.Float, nullable=False)
    width_mm = db.Column(db.Float, nullable=False)
    thickness_mm = db.Column(db.Float, nullable=False)
    weight_kg = db.Column(db.Float, nullable=True)
    cell_count = db.Column(db.Integer, nullable=True)
    cell_type = db.Column(db.String(100), nullable=True)
    efficiency_pct = db.Column(db.Float, nullable=True)
    voc_v = db.Column(db.Float, nullable=True)
    isc_a = db.Column(db.Float, nullable=True)
    vmp_v = db.Column(db.Float, nullable=True)
    imp_a = db.Column(db.Float, nullable=True)
    temp_coeff_pmax_pct = db.Column(db.Float, nullable=True)
    temp_coeff_voc_pct = db.Column(db.Float, nullable=True)
    temp_coeff_isc_pct = db.Column(db.Float, nullable=True)
    max_system_voltage_v = db.Column(db.Float, nullable=True)
    frame_material = db.Column(db.String(80), nullable=True)
    glass_type = db.Column(db.String(120), nullable=True)
    junction_box = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class SolarPanel(db.Model):
    """
    One physical PV module on the roof - 172 total, 86 per roof slope, one
    slope per Solis inverter (each inverter's DC inputs only reach the
    modules wired to its own side - see migration/seed_solar_roof.py, which
    generates all 172 for this shop's 56m x 13m gable roof, 5 rows per side).
    row/col are a grid position within this panel's own side only (both
    1-based), not a real-world coordinate - /admin/solar-roof renders the
    roof as two stacked 5-row grids (inverter_device_id's two distinct
    values), matching the physical two-slope layout.

    string_number (1-8) is which physical string this panel is wired into -
    each inverter has 4 MPPT trackers and each tracker combines 2 strings
    in parallel (string 1&2 -> MPPT1, 3&4 -> MPPT2, 5&6 -> MPPT3, 7&8 ->
    MPPT4), so a tracker's one live reading (_solis_snapshot()'s pv.strings,
    0-indexed there) is necessarily shared by both of its strings - the
    inverter has no way to measure the two halves separately. Nullable
    until assigned via the bulk-select tool on /admin/solar-roof.
    """
    id = db.Column(db.Integer, primary_key=True)
    inverter_device_id = db.Column(db.Integer, db.ForeignKey('modbus_device.id'), nullable=False)
    row = db.Column(db.Integer, nullable=False)
    col = db.Column(db.Integer, nullable=False)
    string_number = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    inverter = db.relationship('ModbusDevice', backref='solar_panels')


class ShellyReadingLog(db.Model):
    """
    Local trail of periodic readings, timestamped as unix seconds (int)
    rather than a DateTime column. Written by the background poller thread
    (see start_shelly_history_poller()) once a minute for every online
    device, regardless of whether the dashboard is open - unlike the meters' own
    history (Gen2 only, ~45-48 day retention), this runs 24/7 as long as the
    app process is up and has no generation split, since it's built from the
    same normalized shelly_fleet_snapshot() the live dashboard already uses.
    """
    id = db.Column(db.Integer, primary_key=True)
    host = db.Column(db.String(100), nullable=False, index=True)
    ts = db.Column(db.Integer, nullable=False, index=True)
    total_power = db.Column(db.Float)
    total_energy = db.Column(db.Float)
    # Raw per-phase snapshot.channels list (json.dumps'd) - voltage/current/
    # act_power/aprt_power/pf/freq per channel, same shape shelly_device_
    # snapshot() already returns for the live dashboard. Kept as JSON rather
    # than one column per phase/field: channel count and labels vary by
    # device (3-phase 3EM vs 2-input Gen1 EM vs Gen2 monophase), same reason
    # Detail.geometry_json etc. use JSON elsewhere in this file. Nullable for
    # rows written before this column existed.
    channels_json = db.Column(db.Text)


class SolisReadingLog(db.Model):
    """
    Same idea as ShellyReadingLog, for a Solis S6 inverter (_solis_snapshot())
    instead of a plain meter - written by start_solis_history_poller() once a
    minute for every online Solis ModbusDevice, 24/7 regardless of whether
    /admin/power is open. A few headline fields get their own column for
    cheap querying; the FULL snapshot (pv/ac/battery/load/meter - everything
    the register map exposes) is also kept as JSON, since "for later
    processing" means not yet knowing which of those fields will actually be
    wanted - narrowing to a handful of columns now would throw the rest away
    permanently.
    """
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey('modbus_device.id'), nullable=False, index=True)
    ts = db.Column(db.Integer, nullable=False, index=True)
    ac_power = db.Column(db.Float)
    pv_power = db.Column(db.Float)
    battery_soc = db.Column(db.Float)
    battery_power = db.Column(db.Float)
    temperature = db.Column(db.Float)
    battery_temperature = db.Column(db.Float)
    battery_fault_bits = db.Column(db.Integer)
    snapshot_json = db.Column(db.Text, nullable=False)

    device = db.relationship('ModbusDevice', backref='reading_logs')


class TemperatureSensor(db.Model):
    """
    A temperature/humidity sensor (Shelly H&T or otherwise) feeding live
    readings via MQTT - see sensor_type below, the MQTT LIVE FEED section
    for the topic shapes, and _mqtt_temp_snapshot(). Separate from
    ShellyDevice (energy meters): a temp
    sensor has no power/channels/panel concept, and "room or other place" is
    its own placement question, not tied to a machine's electrical panel.
    room_id and location_label are both optional and not mutually exclusive
    in the schema (a sensor could technically have both), but the UI only
    ever sets one - room_id for something placed in a mapped Room,
    location_label free text for "other places" the Building/Room hierarchy
    doesn't cover (e.g. outdoors, a specific corner not worth its own Room).
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    mqtt_topic = db.Column(db.String(150), nullable=False, unique=True)
    # Which MQTT topic/payload shape this physical sensor publishes -
    # different brands/generations are NOT interchangeable (see
    # TEMP_SENSOR_TYPES and _handle_mqtt_temp_message()) the same way
    # ModbusDevice.device_type or ShellyDevice generations aren't.
    # 'shelly_ht_gen1' kept as the default so existing rows (all seeded
    # before this field existed) need no backfill.
    sensor_type = db.Column(db.String(20), nullable=False, default='shelly_ht_gen1')
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=True)
    location_label = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    room = db.relationship('Room', backref='temperature_sensors')

    @property
    def place_label(self):
        if self.room:
            return f'{self.room.building.name} / {self.room.name}'
        return self.location_label or '—'


class Convector(db.Model):
    """
    A room's heating/cooling convector, switched via its own dedicated
    Shelly relay (a plain Shelly 1/1PM for Gen1, Shelly Plus/Pro 1/1PM for
    Gen2+) - a separate device from ShellyDevice (energy meters) and
    TemperatureSensor. This is deliberately the ONE device class in the app
    the UI is allowed to actively switch on/off - see
    _shelly_convector_set()/admin_convector_toggle(). Turning an industrial
    Machine's power on/off remotely is a regulated safety question
    (EN 60204-1/ISO 12100 - see the READ-ONLY BY POLICY comment in the
    Shelly section below), but a room convector is an ordinary smart-plug
    action; wiring up control here was confirmed explicitly with the user
    rather than just because the hardware happens to support it.
    device_type picks the HTTP/MQTT payload shape to poll/switch with (see
    CONVECTOR_TYPES) - Gen1's plain /relay/<N> vs Gen2's Switch.* RPC, same
    reasoning as ModbusDevice.device_type/TemperatureSensor.sensor_type.
    connection_type picks HTTP (host) vs MQTT (mqtt_topic) transport for
    both reading status AND switching - same host/mqtt_topic split as
    ShellyDevice, except here MQTT control means this app PUBLISHing a
    command (see _mqtt_publish()), not just subscribing like everywhere
    else MQTT is used.
    room_id/location_label mirror TemperatureSensor's optional-either-way
    placement fields. pos_x/pos_y are this convector's dragged position on
    its room's map (admin_factory_map_room.html), percent of canvas width/
    height - same convention as Machine.pos_x/pos_y - only meaningful when
    room_id is set (a free-location convector just doesn't appear on that
    map).
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    connection_type = db.Column(db.String(10), nullable=False, default='ip')
    host = db.Column(db.String(100), nullable=True)
    mqtt_topic = db.Column(db.String(150), nullable=True, unique=True)
    device_type = db.Column(db.String(20), nullable=False, default='shelly_gen1')
    relay_channel = db.Column(db.Integer, nullable=False, default=0)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=True)
    location_label = db.Column(db.String(150), nullable=True)
    pos_x = db.Column(db.Float, nullable=True)
    pos_y = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    room = db.relationship('Room', backref='convectors')

    @property
    def place_label(self):
        if self.room:
            return f'{self.room.building.name} / {self.room.name}'
        return self.location_label or '—'


class Vehicle(db.Model):
    """
    A company vehicle (see admin_vehicles()). Tracks three recurring legal
    deadlines as plain expiry dates - insurance (ГО - one field, not split
    civil-liability/casco per user confirmation), vignette, and technical
    inspection (преглед). Each is classified fresh on every request by
    vehicle_deadline_status() (ok/warning/expired), never stored as a status
    - so a warning naturally starts VEHICLE_WARNING_DAYS before expiry and
    keeps showing every day after, without any cron job. inject_vehicle_alerts()
    surfaces warning+expired rows app-wide as a navbar banner; this is
    in-app-only for v1, no email, per explicit user choice (an email
    delivery path already exists via send_email() if that's wanted later).
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    license_plate = db.Column(db.String(20), nullable=False, unique=True)
    brand = db.Column(db.String(100), nullable=True)
    model = db.Column(db.String(100), nullable=True)
    vin = db.Column(db.String(50), nullable=True)
    responsible_name = db.Column(db.String(150), nullable=True)
    insurance_expiry = db.Column(db.Date, nullable=True)
    # Whether ГО is paid quarterly rather than as one annual sum - see
    # insurance_installment_dates/next_insurance_installment below and
    # VehicleInsuranceInstallment. Purely a display/reminder toggle; doesn't
    # change insurance_expiry itself (the policy's actual end date).
    insurance_installments = db.Column(db.Boolean, nullable=False, default=False)
    vignette_expiry = db.Column(db.Date, nullable=True)
    inspection_expiry = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def deadlines(self):
        """[{'label','field','date','status','days'}, ...] for the 3 tracked
        deadlines - shared by the listing table and inject_vehicle_alerts()
        so both always agree on what counts as due."""
        rows = []
        for label, field in (
            ('Застраховка (ГО)', 'insurance_expiry'),
            ('Винетка', 'vignette_expiry'),
            ('Технически преглед', 'inspection_expiry'),
        ):
            d = getattr(self, field)
            status, days = vehicle_deadline_status(d)
            rows.append({'label': label, 'field': field, 'date': d, 'status': status, 'days': days})
        return rows

    @property
    def insurance_installment_dates(self):
        """The 4 quarterly ГО due dates for the current policy year,
        calculated purely from insurance_expiry (assumes a 12-month policy
        - expiry minus 12/9/6/3 months) per explicit user choice, rather
        than a separate policy-start field."""
        if not self.insurance_expiry:
            return []
        return [_add_months(self.insurance_expiry, m) for m in (-12, -9, -6, -3)]

    @property
    def next_insurance_installment(self):
        """{'due_date','status','days'} for the earliest quarterly
        installment not yet marked paid (see VehicleInsuranceInstallment),
        or None if installments aren't tracked for this vehicle or all 4
        are already paid. Unlike the 3 fixed deadlines above, 'expired'
        here means unpaid past its due date and keeps showing - per
        explicit user choice - until someone actually ticks it paid
        (admin_vehicle_installment_toggle), not auto-cleared once the next
        quarter starts."""
        if not self.insurance_installments:
            return None
        dues = self.insurance_installment_dates
        if not dues:
            return None
        paid_dates = self.installment_paid_dates
        for d in dues:
            if d not in paid_dates:
                status, days = vehicle_deadline_status(d)
                return {'due_date': d, 'status': status, 'days': days}
        return None

    @property
    def installment_paid_dates(self):
        """Set of due dates already marked paid - used by
        next_insurance_installment and installment_rows below."""
        return {r.due_date for r in self.installment_records if r.paid}

    @property
    def installment_rows(self):
        """[{'due_date','paid','status'}, ...] for all 4 quarterly due
        dates - status is 'ok' once paid, otherwise the normal
        ok/warning/expired classification of that date. Drives the
        per-installment paid checkboxes in admin_vehicles.html."""
        paid_dates = self.installment_paid_dates
        rows = []
        for d in self.insurance_installment_dates:
            paid = d in paid_dates
            status = 'ok' if paid else vehicle_deadline_status(d)[0]
            rows.append({'due_date': d, 'paid': paid, 'status': status})
        return rows


class VehicleInsuranceInstallment(db.Model):
    """
    Payment ('paid' checkbox) tracking for one quarterly ГО installment of a
    Vehicle - see Vehicle.insurance_installment_dates/next_insurance_installment.
    Keyed by (vehicle_id, due_date) rather than an installment index, since
    due dates are always recomputed fresh from Vehicle.insurance_expiry, never
    stored - if insurance_expiry ever changes, the due dates shift and any
    paid marks for the now-stale dates simply stop being matched (harmless
    leftover rows, not worth cleaning up for this scale of data).
    """
    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicle.id'), nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    paid = db.Column(db.Boolean, nullable=False, default=False)
    paid_at = db.Column(db.DateTime, nullable=True)

    vehicle = db.relationship('Vehicle', backref=db.backref('installment_records', cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('vehicle_id', 'due_date', name='uq_vehicle_installment_due'),)


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
    # 'machine' (default, existing sectioned grid) or 'product' (new flat
    # grid on the services page, mirrors the index page's machine layout -
    # see services()/services.html). Products never use section_title.
    kind = db.Column(db.String(20), nullable=False, default='machine')
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


_REGISTRATION_CLOSED_KEY = 'settings.registration_closed'


def registration_closed():
    """Site-wide new-account lock, stored as a '1'/'0' row in the same
    EditableText key/value table used for editable prose - no new table
    needed for one boolean flag. Existing users can still log in; this only
    gates /register."""
    return get_text(_REGISTRATION_CLOSED_KEY, '0') == '1'


class Offer(db.Model):
    """
    A generated sales offer (multi-line quote), rendered as a browser-print
    page matching the shop's standard offer layout (title/address/object
    line, item table, footer terms+total+signature - see
    templates/admin_offer_print.html). Distinct from the older single-Product
    print-to-PDF flow (offer.html/admin_product_offer()) - this one covers an
    arbitrary quote made of any mix of catalog Products/Details and free-form
    text lines (see OfferItem), not just one product.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Zero-padded sequential number (e.g. '00000000200') - see _next_offer_number().
    number = db.Column(db.String(20), unique=True, nullable=False)
    object_title = db.Column(db.String(255), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    footer_notes = db.Column(db.Text, nullable=True)
    signed_by = db.Column(db.String(150), nullable=True)
    # Explicit expiration date shown on the printed offer (e.g. "Валидна до:
    # 30.09.2026"), separate from the free-text footer_notes line some
    # offers used to spell out a validity period in prose (e.g. "Валидност
    # на офертата 15 дни") - this is a real date an admin can pick, not text.
    valid_until = db.Column(db.Date, nullable=True)
    # Whole-offer discount (0-100) applied to the item subtotal - see
    # subtotal/discount_amount/total below and admin_offer_print.html's
    # "price; -sale%, actual price" total breakdown.
    discount_percent = db.Column(db.Float, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    client = db.relationship('Client')
    created_by = db.relationship('User')

    @property
    def subtotal(self):
        """Sum of every line's price before the whole-offer discount."""
        return round(sum(item.line_total for item in self.items), 2)

    @property
    def discount_amount(self):
        return round(self.subtotal * (self.discount_percent or 0) / 100, 2)

    @property
    def total(self):
        """Final amount owed - subtotal minus the whole-offer discount, if any."""
        return round(self.subtotal - self.discount_amount, 2)


class OfferItem(db.Model):
    """
    One line of an Offer, in the same standard field shape (code/name/photo/
    description/dimensions/qty/unit/price) regardless of source: item_type
    'product'/'detail' snapshot a catalog row (name/price frozen at add-time,
    same pattern as OrderItemComponent, so editing the catalog later never
    changes an already-generated offer); item_type 'text' is the identical
    shape but every field is typed by hand instead of pulled from a catalog
    pick - for a one-off line, a note, or a section heading, all of which
    still want the same columns in the exported/printed table. description_html
    allows only bold/italic markup (see sanitize_rich_text()) entered via the
    offer editor's mini toolbar (admin_offer_edit.html).
    """
    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(db.Integer, db.ForeignKey('offer.id'), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    item_type = db.Column(db.String(10), nullable=False)  # 'product', 'detail', 'text'
    # Which catalog row this line was picked from, if any (nullable - 'text'
    # rows and hand-typed product/detail names have neither). Only used to
    # let admin_offer_create_order() turn a selected line back into a real
    # OrderItem; every other display path still uses the frozen name/price
    # columns below, never these.
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    name = db.Column(db.String(255), nullable=True)
    description_html = db.Column(db.Text, nullable=True)
    dimensions = db.Column(db.String(100), nullable=True)
    quantity = db.Column(db.Float, nullable=True)
    unit = db.Column(db.String(20), nullable=True)
    unit_price = db.Column(db.Float, nullable=True)
    # On-disk filename (uuid-prefixed, see _save_upload) of an optional photo
    # for this line - the 'снимка' column in the shop's original offer
    # spreadsheets (see admin_offer_print.html).
    image_filename = db.Column(db.String(255), nullable=True)

    offer = db.relationship('Offer', backref=db.backref(
        'items', order_by='OfferItem.position', cascade='all, delete-orphan'))
    product = db.relationship('Product')
    detail = db.relationship('Detail')

    @property
    def line_total(self):
        return round((self.quantity or 0) * (self.unit_price or 0), 2)


class QualityCheck(db.Model):
    """
    Standalone quality-control inspection ("контрол на качеството") - not
    tied to a specific production batch or order, just a catalog
    Detail/Product picked at inspection time. Same target_type/target_id
    convention as DeliveryNoteItem. Holds one or more QualityMeasurement
    rows (measured value vs nominal +/- tolerance); overall_result is
    derived from those at creation time (see admin_create_quality_check())
    and stored so a failed check surfaces in the history table without
    joining/recomputing every time.
    """
    id = db.Column(db.Integer, primary_key=True)
    target_type = db.Column(db.String(20), nullable=False)  # 'detail' / 'product'
    target_id = db.Column(db.Integer, nullable=False)
    inspector_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    overall_result = db.Column(db.String(10), nullable=False, default='pass')  # 'pass' / 'fail'
    notes = db.Column(db.Text, nullable=True)
    # Whether this inspection was carried out under the ISO 8015 independency
    # principle - purely a reference/interpretation flag (ISO 8015 has no
    # tolerance values of its own to compute against), shown as a badge in
    # the history table.
    iso8015 = db.Column(db.Boolean, nullable=False, default=False)
    # Batch/report header fields, matching the shop's paper "Mechanical
    # Inspection Report" form (see admin_quality_check_print()) - drawing_no
    # is the part's overall drawing number (distinct from each
    # QualityMeasurement.drawing_ref, which is that one dimension's
    # balloon/callout number on the same drawing). sample_size is how many
    # QualitySample columns every QualityMeasurement on this check has -
    # fixed per check, same as the paper form's fixed sample columns.
    drawing_no = db.Column(db.String(50), nullable=True)
    batch_size = db.Column(db.Integer, nullable=True)
    sample_size = db.Column(db.Integer, nullable=False, default=1)
    # Customer/production order number this batch was inspected against -
    # distinct from drawing_no (the part's drawing) and unrelated to this
    # app's own Order model (this may not even correspond to a real Order
    # row - e.g. an external customer PO), so deliberately a free-text
    # field, not a foreign key.
    order_no = db.Column(db.String(50), nullable=True)
    # Comma-joined disposition codes ('accept'/'reject'/'full_inspection'/
    # 'rework'/'deduction'/'other') - the paper report's checkboxes allow
    # more than one to be ticked, so this isn't a single enum value.
    disposition = db.Column(db.String(100), nullable=True)
    # Separate from inspector_id (who performed the measurements) - the
    # paper report has a distinct supervisor sign-off line. Free text since
    # a supervisor need not be a system user account.
    supervisor_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    inspector = db.relationship('User')
    # order_by=id (insertion order) rather than left unordered - the row's
    # position in this list IS its display sequence number (see the "№"
    # column on admin_quality_control.html), never stored as its own column
    # since it's derived purely from list position.
    measurements = db.relationship(
        'QualityMeasurement', backref='check', cascade='all, delete-orphan', lazy=True,
        order_by='QualityMeasurement.id'
    )

    @property
    def target_name(self):
        model = Detail if self.target_type == 'detail' else Product
        row = db.session.get(model, self.target_id)
        label = 'Детайл' if self.target_type == 'detail' else 'Продукт'
        return row.name if row else f'{label} #{self.target_id} (изтрит)'


class MeasuringInstrument(db.Model):
    """
    Catalog of measuring tools (calipers, micrometers, CMM, ...) admins/QC
    staff maintain on their own page (admin_measuring_instruments()) and
    pick per QualityMeasurement row (see instrument_id below) to record
    which tool a given measurement was taken with. accuracy_value/unit is
    purely informational/reference (e.g. 0.01 + 'мм') - never read by any
    pass/fail calculation.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    accuracy_value = db.Column(db.Float, nullable=True)
    accuracy_unit = db.Column(db.String(20), nullable=True)
    # Suggested re-calibration interval (ISO 9001 §7.1.5) - only used to
    # pre-fill the "next due" date when logging a new
    # InstrumentCalibrationRecord; the record's own next_due_date (as
    # actually stated on that calibration's certificate) is always the
    # authoritative source for calibration_status below, not this interval.
    calibration_interval_months = db.Column(db.Integer, nullable=True)

    calibration_records = db.relationship(
        'InstrumentCalibrationRecord', backref='instrument', cascade='all, delete-orphan', lazy=True,
        order_by='InstrumentCalibrationRecord.calibrated_at.desc()'
    )

    @property
    def display_label(self):
        if self.accuracy_value is not None:
            return f'{self.name} (± {self.accuracy_value:g} {self.accuracy_unit or ""})'.strip()
        return self.name

    @property
    def latest_calibration(self):
        return self.calibration_records[0] if self.calibration_records else None

    @property
    def calibration_status(self):
        """'valid' / 'due_soon' (<=30 days) / 'overdue' / 'not_tracked'
        (никога калибриран) - based on the latest record's own next_due_date,
        never recomputed from calibration_interval_months (see that field's
        docstring)."""
        latest = self.latest_calibration
        if not latest or not latest.next_due_date:
            return 'not_tracked'
        days_left = (latest.next_due_date - datetime.utcnow().date()).days
        if days_left < 0:
            return 'overdue'
        if days_left <= 30:
            return 'due_soon'
        return 'valid'


class QualityMeasurement(db.Model):
    """
    One measured dimension row within a QualityCheck (e.g. "φ16c9") -
    nominal value +/- tolerance vs however many QualitySample readings were
    taken (QualityCheck.sample_size of them, one per part in the batch) -
    matches the paper "Mechanical Inspection Report" form's one-row-per-
    dimension, multiple-sample-columns layout (see
    admin_quality_check_print()). instrument_id is optional - which
    MeasuringInstrument (if any) was used for this dimension, picked per-row
    since different dimensions on the same QualityCheck can legitimately be
    measured with different tools (e.g. diameter with a caliper, a critical
    dimension with the CMM).
    """
    id = db.Column(db.Integer, primary_key=True)
    quality_check_id = db.Column(db.Integer, db.ForeignKey('quality_check.id'), nullable=False)
    parameter_name = db.Column(db.String(100), nullable=False)
    # Free-text characteristic category (e.g. "Диаметър", "Ъгъл", "Дължина")
    # - picked from a searchable preset list on the form (see
    # QC_MEASUREMENT_TYPES in admin_quality_control.html) but not a strict
    # enum, so a one-off type not on that list can still be typed in.
    # Purely descriptive/organizational, same as parameter_name - never read
    # by any tolerance calculation.
    measurement_type = db.Column(db.String(50), nullable=True)
    nominal_value = db.Column(db.Float, nullable=False)
    tolerance_plus = db.Column(db.Float, nullable=False, default=0.0)
    tolerance_minus = db.Column(db.Float, nullable=False, default=0.0)
    unit = db.Column(db.String(20), nullable=True)
    # Free-text reference to the dimension's balloon/callout number on the
    # technical drawing (e.g. "5" or "A5") - purely a cross-reference back to
    # the drawing, not used in any calculation.
    drawing_ref = db.Column(db.String(50), nullable=True)
    instrument_id = db.Column(db.Integer, db.ForeignKey('measuring_instrument.id'), nullable=True)

    instrument = db.relationship('MeasuringInstrument')
    samples = db.relationship(
        'QualitySample', backref='measurement', cascade='all, delete-orphan', lazy=True,
        order_by='QualitySample.sample_index'
    )

    @property
    def is_within_tolerance(self):
        """A dimension only passes if every recorded sample does - one bad
        part in the batch fails the whole dimension row."""
        return bool(self.samples) and all(s.is_within_tolerance for s in self.samples)

    @property
    def sample_map(self):
        """{sample_index: QualitySample} - sample_index can have gaps (a
        blank sample cell is simply skipped, not stored), so callers that
        need to place samples into fixed report columns (1..sample_size,
        see admin_quality_check_print.html) look up by index here rather
        than assuming samples[i-1] lines up positionally."""
        return {s.sample_index: s for s in self.samples}


class QualitySample(db.Model):
    """One sample's reading for a QualityMeasurement dimension row - sample_index
    is 1-based, matching the paper form's numbered sample columns (1..sample_size)."""
    id = db.Column(db.Integer, primary_key=True)
    quality_measurement_id = db.Column(db.Integer, db.ForeignKey('quality_measurement.id'), nullable=False)
    sample_index = db.Column(db.Integer, nullable=False)
    value = db.Column(db.Float, nullable=False)

    @property
    def is_within_tolerance(self):
        m = self.measurement
        return (m.nominal_value - m.tolerance_minus) <= self.value <= (m.nominal_value + m.tolerance_plus)

    @property
    def deviation(self):
        return round(self.value - self.measurement.nominal_value, 4)


class QualityCheckTemplate(db.Model):
    """
    Explicit, admin-curated starting point for admin_create_quality_check() -
    at most one per Detail/Product (target_type/target_id), created/replaced
    only when the user clicks "Запиши като темплейт" (see
    admin_save_quality_template()). Deliberately NOT derived from whatever
    QualityCheck was most recently submitted for that target - that drifted
    on every inspection and could leave stale data showing for a target
    that has never had one of its own (see admin_quality_control.html's
    loadTemplateForTarget()). Never holds sample values or per-inspection
    fields (order_no, supervisor_name, notes) - only the reusable structure.
    """
    id = db.Column(db.Integer, primary_key=True)
    target_type = db.Column(db.String(20), nullable=False)
    target_id = db.Column(db.Integer, nullable=False)
    drawing_no = db.Column(db.String(50), nullable=True)
    batch_size = db.Column(db.Integer, nullable=True)
    sample_size = db.Column(db.Integer, nullable=False, default=1)
    iso8015 = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    measurements = db.relationship(
        'QualityCheckTemplateMeasurement', backref='template', cascade='all, delete-orphan', lazy=True,
        order_by='QualityCheckTemplateMeasurement.id'
    )

    __table_args__ = (db.UniqueConstraint('target_type', 'target_id', name='uq_quality_template_target'),)


class QualityCheckTemplateMeasurement(db.Model):
    """One dimension row within a QualityCheckTemplate - same shape as
    QualityMeasurement minus the sample values, since a template only ever
    describes structure, never a measurement result."""
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('quality_check_template.id'), nullable=False)
    parameter_name = db.Column(db.String(100), nullable=False)
    measurement_type = db.Column(db.String(50), nullable=True)
    nominal_value = db.Column(db.Float, nullable=False)
    tolerance_plus = db.Column(db.Float, nullable=False, default=0.0)
    tolerance_minus = db.Column(db.Float, nullable=False, default=0.0)
    unit = db.Column(db.String(20), nullable=True)
    drawing_ref = db.Column(db.String(50), nullable=True)
    instrument_id = db.Column(db.Integer, db.ForeignKey('measuring_instrument.id'), nullable=True)

    instrument = db.relationship('MeasuringInstrument')


class InstrumentCalibrationRecord(db.Model):
    """
    One calibration/verification event for a MeasuringInstrument (ISO 9001
    §7.1.5) - kept permanently (never overwritten) so the instrument's full
    calibration history stays auditable, same pattern as
    ControlledDocumentRevision. next_due_date is read directly off the
    calibration certificate (whatever the calibrating lab/person states),
    not recomputed from MeasuringInstrument.calibration_interval_months -
    real certificates occasionally shorten/extend the next interval.
    """
    id = db.Column(db.Integer, primary_key=True)
    instrument_id = db.Column(db.Integer, db.ForeignKey('measuring_instrument.id'), nullable=False)
    calibrated_at = db.Column(db.Date, nullable=False)
    next_due_date = db.Column(db.Date, nullable=True)
    calibrated_by = db.Column(db.String(150), nullable=True)
    certificate_no = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    original_filename = db.Column(db.String(255), nullable=True)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    recorded_by = db.relationship('User')


CONTROLLED_DOCUMENT_CATEGORIES = {
    'manual': 'Наръчник по качеството',
    'policy': 'Политика',
    'procedure': 'Процедура',
    'work_instruction': 'Работна инструкция',
    'form': 'Формуляр',
    'record': 'Запис',
}
CONTROLLED_DOCUMENT_STATUSES = {
    'draft': 'Чернова',
    'active': 'Активен',
    'obsolete': 'Отменен',
}


class ControlledDocument(db.Model):
    """
    ISO 9001 §7.5 "documented information" - a controlled quality-system
    document (manual/policy/procedure/work instruction/form/record). The
    row's own filename/current_revision always reflect the latest approved
    version; every prior version is kept in ControlledDocumentRevision
    (never overwritten/deleted) so changes stay traceable, per §7.5.3's
    control-of-changes requirement.
    """
    id = db.Column(db.Integer, primary_key=True)
    document_no = db.Column(db.String(50), unique=True, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(30), nullable=False, default='procedure')
    status = db.Column(db.String(20), nullable=False, default='active')
    current_revision = db.Column(db.String(20), nullable=False, default='1')
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_by = db.Column(db.String(100), nullable=True)
    effective_date = db.Column(db.Date, nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    original_filename = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    owner = db.relationship('User')
    revisions = db.relationship(
        'ControlledDocumentRevision', backref='document', cascade='all, delete-orphan', lazy=True,
        order_by='ControlledDocumentRevision.id'
    )

    @property
    def category_label(self):
        return CONTROLLED_DOCUMENT_CATEGORIES.get(self.category, self.category)

    @property
    def status_label(self):
        return CONTROLLED_DOCUMENT_STATUSES.get(self.status, self.status)


class ControlledDocumentRevision(db.Model):
    """
    One historical version of a ControlledDocument, added whenever a new
    revision is uploaded (see admin_add_document_revision()) - the parent
    ControlledDocument's own filename/current_revision are updated to match
    the newest row here, but older rows (and their files on disk) stay
    around for audit trail.
    """
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('controlled_document.id'), nullable=False)
    revision_label = db.Column(db.String(20), nullable=False)
    change_description = db.Column(db.Text, nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    original_filename = db.Column(db.String(255), nullable=True)
    approved_by = db.Column(db.String(100), nullable=True)
    revised_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    revised_by = db.relationship('User')


CAPA_STATUSES = {
    'open': 'Отворен',
    'in_progress': 'В процес',
    'closed': 'Затворен',
    'verified': 'Потвърден',
}


class CapaRecord(db.Model):
    """
    ISO 9001 §10.2 corrective/preventive action record - full workflow from
    problem description through containment, root cause, corrective action,
    preventive action, and effectiveness verification. source_description
    is deliberately free text (not a link to a specific QualityCheck/order/
    complaint row) - a CAPA can originate from an internal nonconformity, a
    customer complaint, an audit finding, or anywhere else, and forcing a
    structured link would mean modeling all of those sources up front.
    """
    id = db.Column(db.Integer, primary_key=True)
    capa_no = db.Column(db.String(20), unique=True, nullable=False)
    source_description = db.Column(db.Text, nullable=True)
    problem_description = db.Column(db.Text, nullable=False)
    containment_action = db.Column(db.Text, nullable=True)
    root_cause = db.Column(db.Text, nullable=True)
    corrective_action = db.Column(db.Text, nullable=True)
    preventive_action = db.Column(db.Text, nullable=True)
    responsible_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    due_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='open')
    verification_notes = db.Column(db.Text, nullable=True)
    verified_by = db.Column(db.String(100), nullable=True)
    verified_at = db.Column(db.Date, nullable=True)
    opened_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    responsible = db.relationship('User', foreign_keys=[responsible_id])
    opened_by = db.relationship('User', foreign_keys=[opened_by_id])

    @property
    def status_label(self):
        return CAPA_STATUSES.get(self.status, self.status)

    @property
    def is_overdue(self):
        return bool(self.due_date and self.status in ('open', 'in_progress') and self.due_date < datetime.utcnow().date())


def _next_capa_number():
    """Sequential 'CAPA-0001' style numbering - same read-then-increment
    tradeoff as _next_offer_number(), fine for this app's single-admin-at-a-
    time usage."""
    last = db.session.query(db.func.max(CapaRecord.capa_no)).scalar()
    next_n = 1
    if last and last.startswith('CAPA-'):
        try:
            next_n = int(last.rsplit('-', 1)[1]) + 1
        except ValueError:
            pass
    return f'CAPA-{next_n:04d}'


AUDIT_STATUSES = {
    'planned': 'Планиран',
    'in_progress': 'В процес',
    'completed': 'Завършен',
}
AUDIT_FINDING_TYPES = {
    'nonconformity': 'Несъответствие',
    'observation': 'Наблюдение',
    'improvement': 'Препоръка за подобрение',
}


class InternalAudit(db.Model):
    """ISO 9001 §9.2 internal audit header - scope, auditor, dates, status,
    overall conclusion. Individual findings live in AuditFinding below."""
    id = db.Column(db.Integer, primary_key=True)
    audit_no = db.Column(db.String(20), unique=True, nullable=False)
    scope = db.Column(db.Text, nullable=False)
    auditor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    planned_date = db.Column(db.Date, nullable=True)
    actual_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='planned')
    summary = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    auditor = db.relationship('User', foreign_keys=[auditor_id])
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    findings = db.relationship(
        'AuditFinding', backref='audit', cascade='all, delete-orphan', lazy=True, order_by='AuditFinding.id'
    )

    @property
    def status_label(self):
        return AUDIT_STATUSES.get(self.status, self.status)


class AuditFinding(db.Model):
    """
    One finding from an InternalAudit (nonconformity/observation/
    improvement opportunity). capa_id is set when "Отвори CAPA" is used to
    hand this finding off to a new CapaRecord (see admin_add_capa()) - a
    finding that already has one shows a link to it instead of the button,
    so the same finding can't spawn a second CAPA by accident.
    """
    id = db.Column(db.Integer, primary_key=True)
    audit_id = db.Column(db.Integer, db.ForeignKey('internal_audit.id'), nullable=False)
    finding_type = db.Column(db.String(20), nullable=False, default='observation')
    description = db.Column(db.Text, nullable=False)
    clause_reference = db.Column(db.String(50), nullable=True)
    capa_id = db.Column(db.Integer, db.ForeignKey('capa_record.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    capa = db.relationship('CapaRecord')

    @property
    def finding_type_label(self):
        return AUDIT_FINDING_TYPES.get(self.finding_type, self.finding_type)


def _next_audit_number():
    last = db.session.query(db.func.max(InternalAudit.audit_no)).scalar()
    next_n = 1
    if last and last.startswith('AUD-'):
        try:
            next_n = int(last.rsplit('-', 1)[1]) + 1
        except ValueError:
            pass
    return f'AUD-{next_n:04d}'


# ----------------- ISO 9001 - ПРЕГЛЕД ОТ РЪКОВОДСТВОТО (§9.3) -----------------

class ManagementReview(db.Model):
    """
    ISO 9001 §9.3 management review record. The four snapshot_* counters are
    captured once, at creation time, from the live CAPA/audit/calibration
    data (see admin_add_management_review()) - deliberately frozen rather
    than recomputed on every view, so a past review's "what did leadership
    see" record doesn't silently change after the fact (e.g. once someone
    closes a CAPA that was still open when the review actually happened).
    Everything else is free text, same rationale as
    CapaRecord.source_description - forcing a fully structured model for
    every ISO §9.3 input (customer feedback, resource adequacy, etc.) would
    mean modeling all of those sources up front for no real benefit.
    """
    id = db.Column(db.Integer, primary_key=True)
    review_date = db.Column(db.Date, nullable=False)
    participants = db.Column(db.String(255), nullable=True)

    snapshot_open_capa_count = db.Column(db.Integer, nullable=False, default=0)
    snapshot_overdue_capa_count = db.Column(db.Integer, nullable=False, default=0)
    snapshot_audit_nonconformity_count = db.Column(db.Integer, nullable=False, default=0)
    snapshot_overdue_calibration_count = db.Column(db.Integer, nullable=False, default=0)

    customer_feedback = db.Column(db.Text, nullable=True)
    process_performance = db.Column(db.Text, nullable=True)
    resource_adequacy = db.Column(db.Text, nullable=True)
    external_internal_changes = db.Column(db.Text, nullable=True)
    risk_opportunity_actions = db.Column(db.Text, nullable=True)
    conclusion = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    created_by = db.relationship('User')
    actions = db.relationship(
        'ManagementReviewAction', backref='review', cascade='all, delete-orphan', lazy=True,
        order_by='ManagementReviewAction.id'
    )


class ManagementReviewAction(db.Model):
    """
    One decision/action item coming out of a ManagementReview - mirrors
    AuditFinding's optional CAPA hand-off (capa_id set when "Отвори CAPA" is
    used, see admin_add_capa()). Deliberately has no status field of its
    own, same as AuditFinding - once an action needs real follow-up it gets
    a CAPA, which is what actually tracks progress/closure.
    """
    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(db.Integer, db.ForeignKey('management_review.id'), nullable=False)
    description = db.Column(db.Text, nullable=False)
    responsible_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    due_date = db.Column(db.Date, nullable=True)
    capa_id = db.Column(db.Integer, db.ForeignKey('capa_record.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    responsible = db.relationship('User')
    capa = db.relationship('CapaRecord')


# ----------------- ISO 9001 - ОБУЧЕНИЕ НА ПЕРСОНАЛА (§7.2) -----------------

TRAINING_TYPES = {
    'induction': 'Въвеждащ инструктаж',
    'on_the_job': 'Инструктаж на работното място',
    'external_course': 'Външен курс',
    'safety': 'Безопасност на труда',
    'quality_procedure': 'Процедура по качеството',
    'equipment_operation': 'Работа с машина/оборудване',
    'other': 'Друго',
}


class TrainingRecord(db.Model):
    """
    ISO 9001 §7.2 competence record - documented evidence that an employee
    received a given training/instruction, plus an evaluation of whether it
    actually worked. trainer_name is free text (not a User FK) since a
    trainer is often an outside course/provider, not necessarily a system
    user - same rationale as CapaRecord.verified_by. valid_until is only set
    for trainings that expire (e.g. a safety certificate needing renewal);
    left blank it means "permanent" competence, not "unknown".
    """
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    topic = db.Column(db.String(200), nullable=False)
    training_type = db.Column(db.String(30), nullable=False, default='other')
    trainer_name = db.Column(db.String(150), nullable=True)
    training_date = db.Column(db.Date, nullable=False)
    valid_until = db.Column(db.Date, nullable=True)
    effectiveness_evaluation = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    original_filename = db.Column(db.String(255), nullable=True)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    employee = db.relationship('User', foreign_keys=[employee_id])
    recorded_by = db.relationship('User', foreign_keys=[recorded_by_id])

    @property
    def training_type_label(self):
        return TRAINING_TYPES.get(self.training_type, self.training_type)

    @property
    def is_expired(self):
        return bool(self.valid_until and self.valid_until < datetime.utcnow().date())


# ----------------- ISO 9001 - РЕГИСТЪР НА РИСКА (§6.1) -----------------

RISK_TYPES = {
    'risk': 'Риск',
    'opportunity': 'Възможност',
}
RISK_CATEGORIES = {
    'process': 'Процес',
    'equipment': 'Оборудване',
    'supplier': 'Доставчик',
    'personnel': 'Персонал',
    'financial': 'Финансов',
    'external': 'Външен фактор',
    'it_data': 'ИТ / данни',
    'other': 'Друго',
}
RISK_STATUSES = {
    'identified': 'Идентифициран',
    'monitoring': 'Наблюдение',
    'mitigated': 'Овладян',
    'accepted': 'Приет',
    'closed': 'Затворен',
}


class RiskRegisterEntry(db.Model):
    """
    ISO 9001 §6.1 risk/opportunity register entry. likelihood/impact are each
    1-5 (standard 5x5 matrix) and multiply into risk_score (1-25), bucketed
    into low/medium/high by risk_level below - the usual way this gets
    presented at a management review/audit. capa_id is an optional hand-off
    to a formal CAPA for entries whose mitigation needs the full corrective/
    preventive-action workflow, same pattern as AuditFinding/
    ManagementReviewAction.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    risk_type = db.Column(db.String(20), nullable=False, default='risk')
    category = db.Column(db.String(20), nullable=False, default='other')
    likelihood = db.Column(db.Integer, nullable=False)
    impact = db.Column(db.Integer, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    mitigation_action = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='identified')
    identified_date = db.Column(db.Date, nullable=False)
    review_date = db.Column(db.Date, nullable=True)
    capa_id = db.Column(db.Integer, db.ForeignKey('capa_record.id'), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    owner = db.relationship('User', foreign_keys=[owner_id])
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    capa = db.relationship('CapaRecord')

    @property
    def risk_type_label(self):
        return RISK_TYPES.get(self.risk_type, self.risk_type)

    @property
    def category_label(self):
        return RISK_CATEGORIES.get(self.category, self.category)

    @property
    def status_label(self):
        return RISK_STATUSES.get(self.status, self.status)

    @property
    def risk_score(self):
        return self.likelihood * self.impact

    @property
    def risk_level(self):
        score = self.risk_score
        if score >= 15:
            return 'high'
        if score >= 7:
            return 'medium'
        return 'low'

    @property
    def risk_level_label(self):
        return {'low': 'Нисък', 'medium': 'Среден', 'high': 'Висок'}[self.risk_level]


# ----------------- ISO 9001 - УДОВЛЕТВОРЕНОСТ НА КЛИЕНТИ (§9.1.2) -----------------

SATISFACTION_SOURCES = {
    'survey': 'Анкета',
    'complaint': 'Оплакване',
    'compliment': 'Похвала',
    'warranty_claim': 'Рекламация',
    'delivery_review': 'Преглед на доставка',
    'other': 'Друго',
}


class CustomerSatisfactionRecord(db.Model):
    """
    ISO 9001 §9.1.2 monitoring of customer perception. Linked to the
    existing Client catalog (used for offers/delivery notes) rather than
    free text, so feedback history rolls up per client - see the module's
    scoping discussion. capa_id is an optional hand-off to a formal CAPA for
    negative feedback needing corrective action, same pattern as
    AuditFinding/ManagementReviewAction/RiskRegisterEntry.
    """
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    source = db.Column(db.String(20), nullable=False, default='survey')
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    order_reference = db.Column(db.String(100), nullable=True)
    feedback_date = db.Column(db.Date, nullable=False)
    capa_id = db.Column(db.Integer, db.ForeignKey('capa_record.id'), nullable=True)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    client = db.relationship('Client')
    capa = db.relationship('CapaRecord')
    recorded_by = db.relationship('User')

    @property
    def source_label(self):
        return SATISFACTION_SOURCES.get(self.source, self.source)


def _next_offer_number():
    """
    Zero-padded 11-digit sequential offer number (e.g. '00000000200') -
    starts at 200, continuing the shop's existing paper-offer numbering.
    ponytail: read-then-use rather than a real DB sequence/lock - same
    tradeoff as _next_erp_number(), fine for this app's single-admin-at-a-time
    usage. Numbers sort lexicographically same as numerically since every
    value is zero-padded to the same width.
    """
    last = db.session.query(db.func.max(Offer.number)).scalar()
    next_n = (int(last) + 1) if last else 200
    return f'{next_n:011d}'


_RICH_TEXT_ALLOWED_TAGS = {'b', 'strong', 'i', 'em'}


class _RichTextSanitizer(HTMLParser):
    """
    Allowlist-only HTML sanitizer for OfferItem.description_html - the offer
    editor's toolbar only exposes Bold/Italic, but the raw markup still comes
    from the browser's contenteditable box, so it must be stripped down to
    just that before being persisted/rendered anywhere else (stored-XSS risk
    otherwise, e.g. a pasted <script> or onerror= attribute - see
    testing/test_security_fixes.py for this project's existing stored-XSS
    regression coverage). A stray <div>/<p> (Chrome wraps each contenteditable
    line in one) becomes a line break instead of being dropped silently.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        # HTMLParser hands <script>/<style> bodies to handle_data as raw
        # CDATA (not parsed as tags), so without this they'd leak straight
        # into the sanitized output as escaped text.
        self._skip_tag = None

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            return
        if tag in self.CDATA_CONTENT_ELEMENTS:
            self._skip_tag = tag
        elif tag in _RICH_TEXT_ALLOWED_TAGS:
            self.out.append(f'<{tag}>')
        elif tag == 'br':
            self.out.append('<br>')

    def handle_startendtag(self, tag, attrs):
        if not self._skip_tag and tag == 'br':
            self.out.append('<br>')

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_tag = None
            return
        if tag in _RICH_TEXT_ALLOWED_TAGS:
            self.out.append(f'</{tag}>')
        elif tag in ('div', 'p'):
            self.out.append('<br>')

    def handle_data(self, data):
        if data and not self._skip_tag:
            self.out.append(html_escape(data))


def sanitize_rich_text(raw):
    """Strips raw contenteditable HTML down to bold/italic/line-breaks only.
    Blank/whitespace-only input normalizes to None."""
    if not raw or not raw.strip():
        return None
    parser = _RichTextSanitizer()
    parser.feed(raw)
    parser.close()
    result = ''.join(parser.out).strip()
    # Trim leading/trailing line breaks left over from contenteditable's
    # div-per-line wrapping, and collapse a result that's now empty tags only.
    result = re.sub(r'^(<br>)+|(<br>)+$', '', result)
    return result or None


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------- DXF ГЕОМЕТРИЯ И ЦЕНИ -----------------

# One-time seed data: used only to populate the MaterialPrice table on first
# run (see seed_material_prices() below). After that, prices are read from
# and edited through the database - NOT from this dict - so admins can
# change them at runtime via the admin panel without a code change/redeploy.
# cost_per_m2 keeps the old real EUR/m2 rates. cutting_speed_mm_per_min
# (mm/min) and pierce_rate_per_min (pierces/min) are placeholder ballpark
# speeds, not measured real-machine numbers - unlike the old flat EUR rates
# they replace, there's no way to back-derive a real speed from what the
# shop used to charge per meter/pierce. Admins should tune these to their
# actual machine/material combos via /admin/materials.
DEFAULT_MATERIAL_SEED = {
    "wood": {"cost_per_m2": 10.00, "cutting_speed_mm_per_min": 3000.0, "pierce_rate_per_min": 60.0,
             "name": "Дървесен материал / МДФ"},
    "steel": {"cost_per_m2": 20.00, "cutting_speed_mm_per_min": 1500.0, "pierce_rate_per_min": 40.0, "name": "Въглеродна стомана"},
    "stainless_steel": {"cost_per_m2": 50.00, "cutting_speed_mm_per_min": 800.0, "pierce_rate_per_min": 25.0,
                        "name": "Неръждаема стомана"},
    "aluminum": {"cost_per_m2": 40.00, "cutting_speed_mm_per_min": 1200.0, "pierce_rate_per_min": 35.0, "name": "Алуминий"},
    "copper": {"cost_per_m2": 120.00, "cutting_speed_mm_per_min": 400.0, "pierce_rate_per_min": 15.0, "name": "Мед"},
    "brass": {"cost_per_m2": 90.00, "cutting_speed_mm_per_min": 500.0, "pierce_rate_per_min": 18.0, "name": "Месинг"},
    "galvanized": {"cost_per_m2": 30.00, "cutting_speed_mm_per_min": 1400.0, "pierce_rate_per_min": 38.0,
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
                cutting_speed_mm_per_min=cfg['cutting_speed_mm_per_min'],
                pierce_rate_per_min=cfg['pierce_rate_per_min']
            ))
    db.session.commit()


def seed_billable_services():
    """
    Seeds one default Service ('Лазерно рязане', 50 EUR/h) so the DXF
    calculator has something to price cuts against out of the box - same
    no-op-if-not-empty pattern as seed_material_prices(). Admins add/edit the
    rest from /admin/services.
    """
    if not Service.query.first():
        db.session.add(Service(name='Лазерно рязане', machine_type='laser', price_per_hour_eur=50.0))
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
            if dtype == 'LWPOLYLINE':
                vertices = [(float(p[0]), float(p[1]), float(p[2])) for p in entity.get_points(format='xyb')]
            else:
                # Old-style POLYLINE has no get_points() - it's not an
                # LWPolyline subclass, its vertices are separate sub-entities
                # (entity.vertices) each with their own dxf.location/bulge.
                # Calling get_points() on it raised AttributeError, silently
                # swallowed below, so any drawing using this (older/other-
                # CAD-exported) entity type analyzed as empty geometry - zero
                # length, blank preview. Only 2D polylines carry a flat
                # cuttable outline; 3D polylines/polymeshes/polyfaces aren't
                # planar cut paths and are skipped like any other shape type
                # we don't handle.
                vertices = [(float(v.dxf.location.x), float(v.dxf.location.y), float(v.dxf.bulge))
                            for v in entity.vertices] if entity.is_2d_polyline else []

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


def _service_time_cost(total_length, pierce_count, material, service):
    """
    One service's share of the time-based cutting+pierce cost - the term
    calculate_cnc_price() applies once (single service) and
    calculate_cnc_price_multi_service() sums across every selected service
    (see the DXF calculator's checkbox picker on upload.html). Standard
    units throughout: length in mm, time in minutes.

    cutting_time_min = total_length (mm) / cutting_speed_mm_per_min
    pierce_time_min = pierce_count / pierce_rate_per_min
    time_cost = (cutting_time_min + pierce_time_min) * (price_per_hour_eur / 60)

    Worked example matching the shop's own spec: 50 EUR/h service, 1.2 mm/min
    cutting speed, 200mm cut -> cutting_time_min = 200/1.2 = 166.67 min,
    time_cost = 166.67 * (50/60) = 138.89 EUR (plus pierce time).
    """
    cutting_time_min = total_length / material.cutting_speed_mm_per_min if material.cutting_speed_mm_per_min else 0.0
    pierce_time_min = pierce_count / material.pierce_rate_per_min if material.pierce_rate_per_min else 0.0
    return (cutting_time_min + pierce_time_min) * (service.price_per_hour_eur / 60.0)


def _material_cost(width, height, material):
    """
    Raw-stock cost for one cut, shared by calculate_cnc_price() and
    calculate_cnc_price_multi_service(). Rods, pipes AND profiles are all
    priced off length alone (height, by the Материал и размери tab's
    Диаметър|Ширина / Дължина convention - see DETAIL_DIMENSION_LABELS in
    detail_dxf_dashboard.html) - all three are bar stock bought and cut by
    the linear meter, so cost scales with how much you cut off, not with
    width * height (for a profile that's cross-section width * length, not a
    real area, same reasoning as diameter * length for rods/pipes). Sheets
    are the only type where cost_per_m2 is genuinely an area rate.
    """
    if material.type in ('rods', 'pipes', 'profiles'):
        return (height / 1000) * material.cost_per_m2
    area_m2 = (width * height) / 1_000_000
    return area_m2 * material.cost_per_m2


def _cost_per_m2_from_unit_price(material_type, unit_price, width, height):
    """
    Inverse of _material_cost(): given the price of one whole stock unit
    (what a supplier's delivery note/invoice actually lists for a sheet/rod/
    pipe/profile) plus its dimensions, derives the €/m² (or €/linear-meter
    for rods/pipes/profiles) rate calculate_cnc_price() actually prices off -
    same math as admin_materials.html's client-side whole-price calculator.
    Returns None when there isn't enough to divide by (missing price or
    dimensions); callers then fall back to treating unit_price as already
    being the rate, same as before this existed.
    """
    if unit_price is None or not height:
        return None
    if material_type in ('rods', 'pipes', 'profiles'):
        return unit_price / (height / 1000)
    if not width:
        return None
    return unit_price / ((width * height) / 1_000_000)


def _detail_material_unit_qty(detail):
    """
    Raw-stock quantity needed for ONE unit of `detail` - m² for sheets/other
    (mirrors _material_cost()'s type branching exactly, just returning
    quantity instead of price), linear meters for rods/pipes/profiles. This
    is the "native unit" _material_available_qty()/_material_stock_delta()
    below convert MaterialPrice.stock_quantity into/out of - see those for
    why stock_quantity itself isn't already in this unit for sheets. Used by
    the production wizard (admin_production_orders()) to size a job's
    material need.
    """
    width, height = detail.effective_width, detail.effective_height
    if detail.material.type in ('rods', 'pipes', 'profiles'):
        return height / 1000.0
    return (width * height) / 1_000_000.0


def _material_sheet_area_m2(material):
    """One raw stock sheet's own area (catalog sheet_width_mm x
    sheet_length_mm, NOT a cut part's size), in m² - None when either isn't
    recorded (legacy rows, or a material where "one sheet" isn't a
    meaningful concept)."""
    if material.sheet_width_mm and material.sheet_length_mm:
        return (material.sheet_width_mm * material.sheet_length_mm) / 1_000_000.0
    return None


def _material_available_qty(material):
    """
    How much of `material` is actually available, in the same native unit
    _detail_material_unit_qty()/planned_material_qty use (m² for sheets/
    other, linear meters for rods/pipes/profiles) - what a planned
    production job's need is compared against (create_production_order()).

    For sheets/other, MaterialPrice.stock_quantity counts whole raw sheets
    on hand, not a running m² total - real inventory is counted (sheets on
    the rack), not measured - so the available area is stock_quantity x one
    sheet's own area (_material_sheet_area_m2), not stock_quantity itself.
    Falls back to treating stock_quantity as already the native unit when no
    sheet size is on record (legacy rows) - dividing by an unknown sheet
    size would be meaningless, not merely imprecise. Linear stock
    (rods/pipes/profiles) is unchanged: it's always been tracked as a
    running length total, not a bar count.
    """
    stock = material.stock_quantity or 0.0
    if material.type not in ('rods', 'pipes', 'profiles'):
        sheet_area_m2 = _material_sheet_area_m2(material)
        if sheet_area_m2:
            return stock * sheet_area_m2
    return stock


def _material_stock_delta(material, native_qty):
    """
    Inverse of _material_available_qty(): converts a material-need/material-
    used figure already expressed in the native unit (m² for sheets/other,
    linear meters for rods/pipes/profiles) into the delta actually applied
    to MaterialPrice.stock_quantity via _bump_stock() - dividing by one
    sheet's own area turns an area figure into the equivalent (possibly
    fractional, e.g. "used 0.6 of a sheet") sheet-count delta. Same
    rods/pipes/profiles and unknown-sheet-size pass-through as
    _material_available_qty() - used by both complete_production_order()
    and delete_production_order() so completing then deleting a job always
    lands stock back at exactly its starting value.
    """
    if material.type not in ('rods', 'pipes', 'profiles'):
        sheet_area_m2 = _material_sheet_area_m2(material)
        if sheet_area_m2:
            return native_qty / sheet_area_m2
    return native_qty


def calculate_cnc_price(width, height, total_length, pierce_count, material_key, service_id):
    """
    Time-based pricing engine for a single service - used by the personal
    DXF-upload calculator (DxfFile, process_dxf_upload()). Material supplies
    the raw-stock cost (area for sheets/pipes/profiles, length alone for rods
    - see _material_cost) and how fast it cuts/pierces; the EUR/hour rate
    comes from the selected Service. total = material_cost + time_cost +
    BASE_SETUP_FEE. NOT used for the Detail catalog - see
    calculate_material_price(), which prices a Detail's base cut as material
    only, with cutting cost captured separately as a length-priced Operation
    (_add_cutting_operation) instead of baked into this total.
    """
    material = MaterialPrice.query.filter_by(key=material_key).first()
    service = db.session.get(Service, service_id) if service_id else None
    if not material or not service:
        return 0.0

    material_cost = _material_cost(width, height, material)
    time_cost = _service_time_cost(total_length, pierce_count, material, service)

    total_calculated_euro = material_cost + time_cost + BASE_SETUP_FEE
    return round(total_calculated_euro, 2)


def calculate_material_price(width, height, material_key):
    """
    Material-only price for a Detail's base cut: just raw-stock cost (see
    _material_cost) - no BASE_SETUP_FEE, no cutting cost. A Detail's cutting
    is priced as its own length-based Operation instead (see
    _add_cutting_operation), not baked into calculated_price; the flat
    per-job setup fee doesn't apply here either - it's a one-off DXF-upload
    charge, not something a reusable catalog part carries. Detail-catalog
    only; the DxfFile/personal-upload calculator keeps using
    calculate_cnc_price()/calculate_cnc_price_multi_service(), which still
    charge BASE_SETUP_FEE + cutting time.
    """
    material = MaterialPrice.query.filter_by(key=material_key).first()
    if not material:
        return 0.0
    return round(_material_cost(width, height, material), 2)


def calculate_cnc_price_multi_service(width, height, total_length, pierce_count, material_key, service_ids):
    """
    Same engine as calculate_cnc_price(), but for the DXF calculator's
    multi-service checkbox selection (see upload.html / process_dxf_upload()
    and DxfFile.services) - the material cost and BASE_SETUP_FEE are still
    charged once, but the cutting+pierce time is priced at EVERY selected
    service's rate and summed, not just one. Lets a job that genuinely spans
    multiple billable processes (e.g. a combined cut+engrave pass) get one
    upload/price instead of forcing an artificial single pick.
    """
    material = MaterialPrice.query.filter_by(key=material_key).first()
    services = Service.query.filter(Service.id.in_(service_ids)).all() if service_ids else []
    if not material or not services:
        return 0.0

    material_cost = _material_cost(width, height, material)
    time_cost = sum(_service_time_cost(total_length, pierce_count, material, s) for s in services)

    total_calculated_euro = material_cost + time_cost + BASE_SETUP_FEE
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


def log_action(text, details=None):
    """
    Call from inside a route - anywhere before the response is returned - to
    give this request's ActivityLog row a precise, human-readable Bulgarian
    description ("Обновен материал ...: цена 12.50 → 13.00 лв/м²") instead of
    the generic "endpoint_name (arg=val)" fallback in _log_activity() below.
    `details` is an optional longer breakdown (e.g. one line per item on a
    delivery note/order) shown when the admin log dashboard expands this row -
    use it where the one-line `text` necessarily drops detail (a count, not
    every line); leave it out where `text` already says everything.
    """
    g.log_action = text
    if details:
        g.log_action_details = details


def describe_changes(label, obj, field_labels):
    """
    Diffs an object's pending SQLAlchemy attribute changes into a Bulgarian
    "label: поле стара_ст → нова_ст, ..." string for log_action(), using
    SQLAlchemy's own dirty-attribute history instead of manually snapshotting
    old values in every route. field_labels is {attribute_name: bg_label}.
    MUST be called after assigning the new values but before db.session.
    commit() (commit expires attribute history). Returns "label (без
    промени)" if none of the tracked fields actually changed.
    """
    changes = []
    state = sa_inspect(obj)
    for attr, bg_label in field_labels.items():
        hist = state.attrs[attr].history
        if not hist.has_changes():
            continue
        old_val = hist.deleted[0] if hist.deleted else None
        new_val = hist.added[0] if hist.added else getattr(obj, attr)
        if old_val != new_val:
            old_display = old_val if old_val is not None else '-'
            new_display = new_val if new_val is not None else '-'
            changes.append(f'{bg_label} {old_display} → {new_display}')
    if not changes:
        return f'{label} (без промени)'
    return f'{label}: ' + ', '.join(changes)


@app.after_request
def _log_activity(response):
    """
    Logs every successful state-changing request by a logged-in user as one
    ActivityLog row - a blanket audit trail instead of requiring a log call
    in every route. Prefers a route-supplied log_action() description; falls
    back to the bare endpoint name for routes that don't set one. GETs are
    excluded (nothing changes on a GET, and it would spam the log with page
    views / polling endpoints like /admin/power/data). Rows are never
    auto-deleted - only admin_log_clear() below removes them, and that
    action ends up logged too.
    """
    if (request.method != 'GET' and current_user.is_authenticated
            and request.endpoint and 200 <= response.status_code < 400):
        action = g.get('log_action')
        if not action:
            action = request.endpoint
            if request.view_args:
                action += ' (' + ', '.join(f'{k}={v}' for k, v in request.view_args.items()) + ')'
        try:
            db.session.add(ActivityLog(
                username=current_user.username, role=current_user.role,
                action=action, details=g.get('log_action_details'),
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
    return response


# ----------------- МАРШРУТИ И ЛОГИКА -----------------

@app.context_processor
def inject_current_year():
    return {'current_year': datetime.now().year}


@app.context_processor
def inject_vehicle_alerts():
    """Exposes vehicle_alerts (warning/expired Vehicle deadlines) to every
    template for a navbar banner - see partials/navbar.html. Recomputed on
    every request from Vehicle.deadlines, so a warning simply keeps showing
    itself every day until the date is renewed; no cron/email involved."""
    if not (current_user.is_authenticated and (current_user.is_admin or current_user.is_worker)):
        return {'vehicle_alerts': []}
    alerts = []
    for v in Vehicle.query.all():
        for d in v.deadlines:
            if d['status'] in ('warning', 'expired'):
                alerts.append({
                    'vehicle_id': v.id, 'vehicle': v.name, 'plate': v.license_plate,
                    'label': d['label'], 'status': d['status'], 'days': d['days'],
                })
        inst = v.next_insurance_installment
        if inst and inst['status'] in ('warning', 'expired'):
            alerts.append({
                'vehicle_id': v.id, 'vehicle': v.name, 'plate': v.license_plate,
                'label': f'Вноска ГО ({inst["due_date"].strftime("%d.%m.%Y")})',
                'status': inst['status'], 'days': inst['days'],
            })
    alerts.sort(key=lambda a: a['days'])
    return {'vehicle_alerts': alerts}


def _format_material_dims(material):
    """Width/length/thickness as "Wmm, Lmm, Tmm" - "-" per blank slot. Shared
    by format_material_option (which adds brand/#ID around this) and the
    storage_materials() group-header label (which doesn't - see there)."""
    return ', '.join(f"{dim:g}mm" if dim is not None else '-' for dim in
                      (material.sheet_width_mm, material.sheet_length_mm, material.thickness_mm))


def format_material_option(material):
    """
    Standardized display text for every material <select> in the app:
    "#ID Name (Brand, Width mm, Length mm, Thickness mm)" - always all four
    parenthesized slots, with a "-" placeholder for whichever of brand/
    width/length/thickness is blank/None, so every option in a dropdown
    lines up in the same shape regardless of how much data that particular
    row has. The "#ID" prefix (material.id, not erp_number/code_number) is
    skipped for an unsaved/unflushed row (id is still None) rather than
    printing "#None ".
    """
    id_prefix = f"#{material.id} " if material.id is not None else ''
    return f"{id_prefix}{localized(material, 'display_name')} ({material.brand or '-'}, {_format_material_dims(material)})"


app.jinja_env.globals['get_text'] = get_text
# MATERIAL_TYPE_LABELS values are looked up dynamically (by type_key), so
# pybabel can't statically extract them from this lambda - their EN/DE
# translations are added by hand in translations/*/LC_MESSAGES/messages.po.
app.jinja_env.globals['material_type_label'] = lambda key: gettext(MATERIAL_TYPE_LABELS.get(key, key))
app.jinja_env.globals['MATERIAL_TYPE_LABELS'] = MATERIAL_TYPE_LABELS
app.jinja_env.globals['format_material_option'] = format_material_option
app.jinja_env.globals['material_dimension_labels'] = material_dimension_labels
app.jinja_env.globals['material_price_m2_label'] = material_price_m2_label
app.jinja_env.globals['format_cut_dimensions'] = format_cut_dimensions
app.jinja_env.globals['material_available_qty'] = _material_available_qty


@app.route('/favicon.ico')
def favicon():
    # Browsers request this exact path as a fallback regardless of the
    # <link rel="icon"> tags in <head> (see templates/partials/favicon.html);
    # without this route it 404s since static files only serve under /static/.
    return send_from_directory(
        os.path.join(app.static_folder, 'img', 'favicon'), 'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )


_ROBOTS_EASTER_EGG = """
#                         __/\\__
#                         \\    /
#                   __/\\__/    \\__/\\__
#                   \\                /
#                   /_              _\\
#                     \\            /
#       __/\\__      __/            \\__      __/\\__
#       \\    /      \\                /      \\    /
# __/\\__/    \\__/\\__/                \\__/\\__/    \\__/\\__
# \\                                                    /
# /_                                                  _\\
#   \\                                                /
# __/                                                \\__
# \\                                                    /
# /_  __                                          __  _\\
#   \\/  \\                                        /  \\/
#       /_                                      _\\
#         \\                                    /
#       __/                                    \\__
#       \\                                        /
# __/\\__/                                        \\__/\\__
# \\                                                    /
# /_                                                  _\\
#   \\                                                /
# __/                                                \\__
# \\                                                    /
# /_  __      __  __                  __  __      __  _\\
#   \\/  \\    /  \\/  \\                /  \\/  \\    /  \\/
#       /_  _\\      /_              _\\      /_  _\\
#         \\/          \\            /          \\/
#                   __/            \\__
#                   \\                /
#                   /_  __      __  _\\
#                     \\/  \\    /  \\/
#                         /_  _\\
#                           \\/
"""


@app.route('/robots.txt')
def robots_txt():
    # Public marketing pages (/, /services, /about, /contact) are the only
    # ones worth indexing - everything else requires login anyway. Just
    # allow everything rather than maintaining a path list here.
    body = _ROBOTS_EASTER_EGG + '\nUser-agent: *\nAllow: /\nSitemap: ' + request.url_root.rstrip('/') + '/sitemap.xml\n'
    return app.response_class(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    # Only the public marketing pages are worth listing - everything else
    # requires login, so a crawler can't do anything with those URLs anyway.
    pages = ['/', '/services', '/about', '/contact']
    root = request.url_root.rstrip('/')
    urls = ''.join(f'<url><loc>{root}{p}</loc></url>' for p in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return app.response_class(xml, mimetype='application/xml')


@app.route('/set-language/<lang_code>')
def set_language(lang_code):
    if lang_code in SUPPORTED_LANGUAGES:
        session['language'] = lang_code
    return redirect(request.referrer or url_for('index'))


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
    cards = ServiceMachineCard.query.filter_by(page='services', kind='machine').order_by(ServiceMachineCard.id).all()
    sections = _group_service_cards_by_section(cards)
    # Products are a flat grid (like the index page's machine cards), not
    # sectioned by section_title - see ServiceMachineCard.kind.
    product_cards = ServiceMachineCard.query.filter_by(page='services', kind='product').order_by(ServiceMachineCard.id).all()
    billable_services = Service.query.order_by(Service.name).all()
    return render_template('services.html', active_page='services', machine_sections=sections,
                            product_cards=product_cards, billable_services=billable_services)


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
    services = Service.query.order_by(Service.name).all()
    return render_template('generator.html', materials=materials, services=services, active_page='generator')


@app.route('/api/generator-presets')
@login_required
def api_generator_presets_list():
    """The current user's own saved Panel Generator presets, for the picker on /generator."""
    presets = GeneratorPreset.query.filter_by(user_id=current_user.id).order_by(GeneratorPreset.name).all()
    return jsonify({'status': 'success', 'presets': [
        {'id': p.id, 'name': p.name, 'settings': json.loads(p.settings_json)} for p in presets
    ]})


@app.route('/api/generator-presets', methods=['POST'])
@login_required
def api_generator_presets_save():
    """Save (or overwrite, if the name already exists for this user) a Panel Generator preset."""
    name = request.form.get('name', '').strip()
    settings_json = request.form.get('settings_json', '')
    if not name:
        return jsonify({'status': 'error', 'message': gettext('Моля въведете име на пресет.')}), 400
    try:
        json.loads(settings_json)
    except ValueError:
        return jsonify({'status': 'error', 'message': gettext('Невалидни настройки.')}), 400

    preset = GeneratorPreset.query.filter_by(user_id=current_user.id, name=name).first()
    if preset:
        preset.settings_json = settings_json
    else:
        preset = GeneratorPreset(name=name, settings_json=settings_json, user_id=current_user.id)
        db.session.add(preset)
    db.session.commit()
    return jsonify({'status': 'success', 'id': preset.id})


@app.route('/api/generator-presets/<int:preset_id>/delete', methods=['POST'])
@login_required
def api_generator_presets_delete(preset_id):
    preset = GeneratorPreset.query.get_or_404(preset_id)
    if preset.user_id != current_user.id:
        return jsonify({'status': 'error', 'message': gettext('Нямате достъп.')}), 403
    db.session.delete(preset)
    db.session.commit()
    return jsonify({'status': 'success'})


def _generator_hole_polygon(hole_type, rad):
    """
    Same shape-outline math as shapeOutlineLoops()/buildDxfString() in
    templates/generator.html, kept in sync by hand since it's static
    per-shape geometry (not the randomized layout, which stays client-side).
    Returns a list of point-loops - more than one only for 'hexcluster'.
    """
    if hole_type == 'square':
        return [[(-rad, -rad), (rad, -rad), (rad, rad), (-rad, rad)]]
    if hole_type == 'hexagon':
        return [[(rad * math.cos(i * math.pi / 3), rad * math.sin(i * math.pi / 3)) for i in range(6)]]
    if hole_type == 'triangle':
        return [[(0, -rad), (rad * 0.866, rad / 2), (-rad * 0.866, rad / 2)]]
    if hole_type == 'rhombus':
        return [[(0, -rad), (rad / 1.5, 0), (0, rad), (-rad / 1.5, 0)]]
    if hole_type == 'hexcluster':
        shrink = 0.88
        r = rad * 2 / (1 + shrink)
        v = [(r * math.cos(k * math.pi / 3), r * math.sin(k * math.pi / 3)) for k in range(6)]
        loops = []
        for a, b, c in ((0, 1, 2), (2, 3, 4), (4, 5, 0)):
            pts = [(0, 0), v[a], v[b], v[c]]
            cx = sum(p[0] for p in pts) / 4
            cy = sum(p[1] for p in pts) / 4
            loops.append([(cx + (x - cx) * shrink, cy + (y - cy) * shrink) for x, y in pts])
        return loops
    return []


@app.route('/api/generator/dxf', methods=['POST'])
@login_required
def api_generator_dxf():
    """
    Builds the Panel Generator's export server-side with ezdxf instead of
    the hand-rolled DXF text this used to build in JS. The hand-rolled
    version skipped several sections/tables (BLOCK_RECORD, OBJECTS, entity
    handles) that ezdxf's writer always includes correctly - our own reader
    (ezdxf.readfile, used by analyze_dxf_geometry) is lenient enough to open
    it anyway, but real CAD software is stricter and was failing on it.
    """
    data = request.get_json(silent=True) or {}
    try:
        width = float(data.get('width'))
        height = float(data.get('height'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Невалидни размери.'}), 400
    holes = data.get('holes') or []

    doc = ezdxf.new('R2000')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height)], close=True)

    for hole in holes:
        try:
            hx, hy, size, rot = float(hole['x']), float(hole['y']), float(hole['size']), float(hole.get('rot', 0))
        except (KeyError, TypeError, ValueError):
            continue
        rad = size / 2
        if hole.get('type') == 'circle':
            msp.add_circle((hx, hy), rad)
            continue
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        for loop in _generator_hole_polygon(hole.get('type'), rad):
            pts = [(hx + x * cos_r - y * sin_r, hy + x * sin_r + y * cos_r) for x, y in loop]
            if pts:
                msp.add_lwpolyline(pts, close=True)

    buf = io.StringIO()
    doc.write(buf)
    mem = io.BytesIO(buf.getvalue().encode('utf-8'))
    mem.seek(0)
    filename = f"Panel_{width:g}x{height:g}_Mixed.dxf"
    return send_file(mem, mimetype='application/dxf', as_attachment=True, download_name=filename)


@app.route('/admin/generator-presets')
@role_required('admin')
def admin_generator_presets():
    """Admin-only dashboard listing every user's saved Panel Generator presets."""
    presets = GeneratorPreset.query.order_by(GeneratorPreset.created_at.desc()).all()
    presets_view = [{'preset': p, 'settings': json.loads(p.settings_json)} for p in presets]
    return render_template('admin_generator_presets.html', presets_view=presets_view, active_page='admin_generator_presets')


@app.route('/admin/generator-presets/<int:preset_id>/copy', methods=['POST'])
@role_required('admin')
def admin_generator_preset_copy(preset_id):
    """Copies another user's preset into the current admin's own account (own row, own name)."""
    source = GeneratorPreset.query.get_or_404(preset_id)
    name = source.name
    if GeneratorPreset.query.filter_by(user_id=current_user.id, name=name).first():
        name = f'{name} (копие)'
    copy = GeneratorPreset(name=name, settings_json=source.settings_json, user_id=current_user.id)
    db.session.add(copy)
    db.session.commit()
    flash(f'Пресет "{source.name}" е запазен във вашия акаунт.', 'success')
    return redirect(url_for('admin_generator_presets'))


# ----- EMAIL DELIVERY -----
# Plain stdlib smtplib. Defaults to localhost:25 with no auth/TLS - i.e.
# it hands the message to whatever mail transport is running on this same
# server (e.g. a bare `postfix` install) and lets that do the actual
# delivery, no third-party account needed. Set SMTP_HOST/PORT/USER/PASSWORD
# later to point at a real relay (Resend, Mailgun, etc.) instead - auth and
# STARTTLS only kick in once SMTP_USER is set, so switching providers is an
# env-var change, not a code change.
# Never raises - a delivery failure logs and is swallowed, since the caller
# (forgot_password()) must show the same message either way, to avoid
# leaking which emails are registered.
def send_email(to_addr, subject, body):
    host = os.environ.get('SMTP_HOST', 'localhost')
    port = int(os.environ.get('SMTP_PORT', '25'))
    username = os.environ.get('SMTP_USER')
    password = os.environ.get('SMTP_PASSWORD')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.environ.get('SMTP_FROM') or username or 'no-reply@trafcombg.com'
    msg['To'] = to_addr
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            if username:
                server.starttls()
                server.login(username, password)
            server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        print(f"[email] failed to send to {to_addr} via {host}:{port}: {e}")
        return False


PASSWORD_RESET_MAX_AGE = 3600  # seconds


def _reset_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='password-reset')


EMAIL_VERIFY_MAX_AGE = 3 * 24 * 3600  # 3 days


def _verify_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='email-verify')


def _send_verification_email(user):
    token = _verify_serializer().dumps(user.id)
    verify_url = url_for('verify_email', token=token, _external=True)
    send_email(
        user.email,
        'Потвърдете имейл адреса си - TRAFCOM',
        f'Здравейте, {user.username},\n\n'
        'Натиснете връзката по-долу, за да потвърдите имейл адреса си. '
        f'Връзката е валидна 3 дни:\n\n{verify_url}\n\n'
        'Ако не сте заявили това, просто игнорирайте писмото.\n\n'
        '---\n'
        'Това е автоматично съобщение. Моля, не отговаряйте на този имейл.'
    )


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
            if user.totp_secret:
                session['pending_2fa_user_id'] = user.id
                return redirect(url_for('login_2fa'))
            session.permanent = True
            login_user(user)
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
        flash(gettext('Невалидно потребителско име или парола.'))
    return render_template('login.html')


@app.route('/login/2fa', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login_2fa():
    user_id = session.get('pending_2fa_user_id')
    user = db.session.get(User, user_id) if user_id else None
    if not user or not user.totp_secret:
        session.pop('pending_2fa_user_id', None)
        return redirect(url_for('login'))

    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            session.pop('pending_2fa_user_id', None)
            session.permanent = True
            login_user(user)
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
        flash(gettext('Невалиден код.'), 'danger')
    return render_template('login_2fa.html')


@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email, _ = _validate_email(request.form.get('email'))
        user = User.query.filter_by(email=email).first() if email else None
        if user:
            token = _reset_serializer().dumps(user.id)
            reset_url = url_for('reset_password', token=token, _external=True)
            send_email(
                user.email,
                'Възстановяване на парола - TRAFCOM',
                f'Здравейте, {user.username},\n\n'
                'Натиснете връзката по-долу, за да зададете нова парола. '
                f'Връзката е валидна 1 час:\n\n{reset_url}\n\n'
                'Ако не сте заявили това, просто игнорирайте писмото.\n\n'
                '---\n'
                'Това е автоматично съобщение. Моля, не отговаряйте на този имейл.'
            )
        # Same message whether or not the email exists - the form must not
        # be usable to enumerate registered addresses.
        flash(gettext('Ако имейлът съществува в системата, изпратихме връзка за възстановяване на паролата.'), 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    try:
        user_id = _reset_serializer().loads(token, max_age=PASSWORD_RESET_MAX_AGE)
    except SignatureExpired:
        flash(gettext('Връзката за възстановяване е изтекла. Заявете нова.'), 'danger')
        return redirect(url_for('forgot_password'))
    except BadSignature:
        flash(gettext('Невалидна връзка за възстановяване.'), 'danger')
        return redirect(url_for('forgot_password'))

    user = db.session.get(User, user_id)
    if not user:
        flash(gettext('Невалидна връзка за възстановяване.'), 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        if len(password) < 8:
            flash(gettext('Паролата трябва да бъде поне 8 символа.'), 'danger')
            return render_template('reset_password.html', token=token)
        if password != password_confirm:
            flash(gettext('Паролите не съвпадат.'), 'danger')
            return render_template('reset_password.html', token=token)
        user.password = generate_password_hash(password, method='scrypt')
        db.session.commit()
        flash(gettext('Паролата е сменена успешно. Влезте с новата парола.'), 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)


@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if registration_closed():
        flash(gettext('Сайтът е в момента на техническа поддръжка. Регистрацията на нови профили е временно спряна.'), 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password', '')
        email, email_error = _validate_email(request.form.get('email'))

        if not email:
            flash(email_error or gettext('Имейл е задължителен.'), 'danger')
            return redirect(url_for('register'))

        # Same 8-character floor migration/change_admin_password.py already enforces
        # for the admin account - short passwords are well within the
        # per-minute brute-force budget /login's rate limit still allows.
        if len(password) < 8:
            flash(gettext('Паролата трябва да бъде поне 8 символа.'), 'danger')
            return redirect(url_for('register'))

        # Hash the password
        hashed_password = generate_password_hash(password, method='scrypt')

        # Create user with default role 'regular_user'
        new_user = User(
            username=username,
            password=hashed_password,
            email=email,
            role='regular_user'  # Make sure this is 'role', not 'is_admin'
        )

        db.session.add(new_user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(gettext('Това потребителско име или имейл вече съществува.'), 'danger')
            return redirect(url_for('register'))

        _send_verification_email(new_user)
        flash(gettext('Регистрацията е успешна! Проверете имейла си, за да потвърдите адреса.'), 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/verify-email/<token>')
def verify_email(token):
    try:
        user_id = _verify_serializer().loads(token, max_age=EMAIL_VERIFY_MAX_AGE)
    except SignatureExpired:
        flash(gettext('Връзката за потвърждение е изтекла. Заявете нова от профила си.'), 'danger')
        return redirect(url_for('login'))
    except BadSignature:
        flash(gettext('Невалидна връзка за потвърждение.'), 'danger')
        return redirect(url_for('login'))

    user = db.session.get(User, user_id)
    if not user:
        flash(gettext('Невалидна връзка за потвърждение.'), 'danger')
        return redirect(url_for('login'))

    user.email_verified = True
    db.session.commit()
    flash(gettext('Имейлът е потвърден успешно.'), 'success')
    return redirect(url_for('account') if current_user.is_authenticated else url_for('login'))


@app.route('/account')
@login_required
def account():
    return render_template('account.html')


@app.route('/account/email', methods=['POST'])
@login_required
def account_update_email():
    email, error = _validate_email(request.form.get('email'))
    if not email:
        flash(error or gettext('Имейл е задължителен.'), 'danger')
        return redirect(url_for('account'))
    current_user.email = email
    current_user.email_verified = False
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(gettext('Този имейл вече се използва от друг акаунт.'), 'danger')
        return redirect(url_for('account'))
    _send_verification_email(current_user)
    flash(gettext('Имейлът е обновен. Изпратихме връзка за потвърждение на новия адрес.'), 'success')
    return redirect(url_for('account'))


@app.route('/account/email/resend-verification', methods=['POST'])
@login_required
@limiter.limit("5 per hour")
def account_resend_verification():
    if current_user.email_verified:
        flash(gettext('Имейлът вече е потвърден.'), 'success')
        return redirect(url_for('account'))
    if not current_user.email:
        flash(gettext('Нямате зададен имейл.'), 'danger')
        return redirect(url_for('account'))
    _send_verification_email(current_user)
    flash(gettext('Изпратихме нова връзка за потвърждение.'), 'success')
    return redirect(url_for('account'))


@app.route('/account/2fa/setup', methods=['GET', 'POST'])
@login_required
def account_2fa_setup():
    if current_user.totp_secret:
        flash(gettext('Двуфакторното удостоверяване вече е активирано.'), 'danger')
        return redirect(url_for('account'))

    if request.method == 'POST':
        secret = session.get('pending_totp_secret')
        code = (request.form.get('code') or '').strip()
        if not secret or not pyotp.TOTP(secret).verify(code, valid_window=1):
            flash(gettext('Невалиден код. Опитайте отново.'), 'danger')
            return redirect(url_for('account_2fa_setup'))
        current_user.totp_secret = secret
        db.session.commit()
        session.pop('pending_totp_secret', None)
        flash(gettext('Двуфакторното удостоверяване е активирано.'), 'success')
        return redirect(url_for('account'))

    # Reuse a secret already pending in this session so refreshing the page
    # (or a failed code) doesn't invalidate the QR code the user just scanned.
    secret = session.get('pending_totp_secret') or pyotp.random_base32()
    session['pending_totp_secret'] = secret
    otp_uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.username, issuer_name='Trafcom CNC Портал')
    qr_img = qrcode.make(otp_uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    qr_img.save(buf)
    return render_template('account_2fa_setup.html', secret=secret, qr_svg=buf.getvalue().decode('utf-8'))


@app.route('/account/2fa/disable', methods=['POST'])
@login_required
def account_2fa_disable():
    if not check_password_hash(current_user.password, request.form.get('password', '')):
        flash(gettext('Грешна парола.'), 'danger')
        return redirect(url_for('account'))
    current_user.totp_secret = None
    db.session.commit()
    session.pop('pending_totp_secret', None)
    flash(gettext('Двуфакторното удостоверяване е изключено.'), 'success')
    return redirect(url_for('account'))


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
    correctly for whatever context they place it in (see library.html's
    data-filename attribute), but a stored value that's already free of
    quotes/angle-brackets/control-chars can't be used to break out of any
    context in the first place.
    """
    return _UNSAFE_FILENAME_CHARS.sub('', filename)


# Окончателно възстановен маршут за потребителското табло
def process_dxf_upload(file, material_key, service_ids, machine_id=None):
    """
    Shared DXF-upload pipeline used by both /dashboard and /upload: saves
    the file to a temp path, extracts geometry, validates the material +
    service(s), and builds a (not-yet-committed) DxfFile record with its
    calculated price. Keeping this logic in one place means a future fix to
    it automatically applies to both routes, instead of having to be made
    twice.

    service_ids is a list - see calculate_cnc_price_multi_service()/
    DxfFile.services. At least one valid service is required.

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

        services = Service.query.filter(Service.id.in_(service_ids)).all() if service_ids else []
        if not services:
            return None, None, 'Моля изберете поне една услуга.'

        price = calculate_cnc_price_multi_service(width, height, total_length, pierce_count, material_key, service_ids)

        dxf_file = DxfFile(
            filename=sanitize_display_filename(file.filename),
            material=material_key,
            width=width,
            height=height,
            total_length=total_length,
            calculated_price=price,
            user_id=current_user.id,
            geometry_json=json.dumps(shapes),
            machine_id=machine_id,
            services=services
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
    services = Service.query.order_by(Service.name).all()
    return render_template('library.html', uploads=user_uploads, materials=materials, services=services, active_page='dashboard')


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
            flash(gettext("Моля, изберете файл за качване."), "danger")
            return redirect(request.url)

        if not file.filename.lower().endswith('.dxf'):
            flash(gettext('Невалиден формат! Системата приема само .dxf файлове.'), 'danger')
            return redirect(request.url)

        try:
            chosen_material = request.form.get('material', 'steel')
            chosen_services = [int(v) for v in request.form.getlist('service_ids') if v.isdigit()]
            machine_id_raw = request.form.get('machine_id', '')
            selected_machine = int(machine_id_raw) if machine_id_raw and machine_id_raw.isdigit() else None

            dxf_file, _pierce_count, error = process_dxf_upload(
                file, chosen_material, chosen_services, machine_id=selected_machine
            )
            if error:
                flash(error, 'danger')
                return redirect(request.url)

            db.session.add(dxf_file)
            db.session.commit()
            flash(gettext('Файлът "%(filename)s" беше качен и обработен успешно!', filename=file.filename), 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.rollback()
            flash(gettext('Критична грешка при обработка/запис: %(error)s', error=str(e)), 'danger')
            return redirect(request.url)

    machines = Machine.query.all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    services = Service.query.order_by(Service.name).all()
    return render_template('upload.html', machines=machines, materials=materials, services=services, active_page='upload')

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
        'services': Service.query.count(),
        'clients': Client.query.count(),
        'deliverers': Deliverer.query.count(),
        'suppliers': Supplier.query.count(),
        'orders': Order.query.count(),
        'quality_checks': QualityCheck.query.count(),
        'documents': ControlledDocument.query.count(),
        'capa': CapaRecord.query.count(),
        'audits': InternalAudit.query.count(),
        'management_reviews': ManagementReview.query.count(),
        'training_records': TrainingRecord.query.count(),
        'risk_entries': RiskRegisterEntry.query.count(),
        'satisfaction_records': CustomerSatisfactionRecord.query.count(),
    }


    return render_template('admin.html', counts=counts, active_page='admin')


@app.route('/admin/users')
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    all_users = User.query.filter(User.id != current_user.id).all()
    return render_template('admin_users.html', users=all_users, registration_closed=registration_closed(), active_page='admin_users')


@app.route('/admin/users/toggle-registration', methods=['POST'])
@login_required
def admin_toggle_registration():
    if not current_user.is_admin:
        return jsonify({'error': 'Неоторизиран достъп'}), 403

    closed = request.form.get('registration_closed') == '1'
    row = db.session.get(EditableText, _REGISTRATION_CLOSED_KEY)
    if row:
        row.content = '1' if closed else '0'
    else:
        db.session.add(EditableText(key=_REGISTRATION_CLOSED_KEY, content='1' if closed else '0'))
    db.session.commit()
    log_action('Регистрацията на нови профили беше ' + ('спряна' if closed else 'възобновена'))
    flash('Регистрацията на нови профили е ' + ('спряна.' if closed else 'отворена отново.'), 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/log')
@role_required('admin')
def admin_log():
    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).all()
    return render_template('admin_log.html', logs=logs, active_page='admin_log')


@app.route('/admin/log/clear', methods=['POST'])
@role_required('admin')
def admin_log_clear():
    ActivityLog.query.delete()
    db.session.commit()
    flash('Логът е изчистен.', 'success')
    return redirect(url_for('admin_log'))


@app.route('/admin/log/export')
@role_required('admin')
def admin_log_export():
    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Log'
    ws.append(['Време', 'Потребител', 'Роля', 'Действие'])
    for entry in logs:
        ws.append([entry.timestamp.strftime('%Y-%m-%d %H:%M:%S'), entry.username, entry.role, entry.action])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f'activity_log_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/admin/materials')
@login_required
def admin_materials():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    return render_template('admin_materials.html', materials=materials, active_page='admin_materials')


@app.route('/admin/materials/<int:material_id>/history')
@role_required(['admin', 'worker'])
def admin_material_history(material_id):
    """
    Every recorded stock movement for one material: incoming DeliveryNoteItem
    rows (from a Supplier), outgoing ClientDeliveryNoteItem rows (to a
    Client) - both keyed on the same target_type='material'/target_id
    convention as print_label()/erp_lookup() - plus 'done' ProductionOrder
    rows against this material, split into up to two movements each: the
    original "taken for production" (at completed_at) and, if the job was
    later deleted/undone (see ProductionOrder.reversed_at), a "returned from
    production" (at reversed_at). Quantities are shown in the stock's own
    unit (_material_stock_delta()'s output - sheet count for sheets/other,
    not the raw m² actually used/returned) so they add up against
    stock_quantity the same way delivery-note quantities do. Same
    admin+worker access as storage_materials() (the "склад" page this is
    linked from) and admin_delivery_notes().
    """
    material = MaterialPrice.query.get_or_404(material_id)

    incoming = (DeliveryNoteItem.query.join(DeliveryNote)
                .filter(DeliveryNoteItem.target_type == 'material', DeliveryNoteItem.target_id == material_id)
                .all())
    outgoing = (ClientDeliveryNoteItem.query.join(ClientDeliveryNote)
                .filter(ClientDeliveryNoteItem.target_type == 'material', ClientDeliveryNoteItem.target_id == material_id)
                .all())
    production_jobs = ProductionOrder.query.filter_by(material_id=material_id, status='done').all()

    movements = [
        {'date': item.delivery_note.note_date or item.delivery_note.created_at.date(),
         'created_at': item.delivery_note.created_at,
         'kind': 'delivery', 'direction': 'in', 'quantity': item.quantity,
         'party': item.delivery_note.supplier.name if item.delivery_note.supplier else '-',
         'note_number': item.delivery_note.note_number, 'unit_price': item.unit_price, 'notes': item.notes}
        for item in incoming
    ] + [
        {'date': item.client_delivery_note.note_date or item.client_delivery_note.created_at.date(),
         'created_at': item.client_delivery_note.created_at,
         'kind': 'client', 'direction': 'out', 'quantity': item.quantity,
         'party': item.client_delivery_note.client.name if item.client_delivery_note.client else '-',
         'note_number': item.client_delivery_note.note_number, 'unit_price': item.unit_price, 'notes': item.notes}
        for item in outgoing
    ]
    for job in production_jobs:
        taken_qty = _material_stock_delta(material, job.actual_material_qty)
        completed_by = job.completed_by.username if job.completed_by else '-'
        movements.append({
            'date': job.completed_at.date(), 'created_at': job.completed_at,
            'kind': 'production_take', 'direction': 'out', 'quantity': taken_qty,
            'party': 'Производство', 'note_number': None, 'unit_price': None,
            'notes': f'{job.quantity} бр. "{job.detail.name}" ({completed_by})',
        })
        if job.reversed_at is not None:
            reversed_by = job.reversed_by.username if job.reversed_by else '-'
            movements.append({
                'date': job.reversed_at.date(), 'created_at': job.reversed_at,
                'kind': 'production_return', 'direction': 'in', 'quantity': taken_qty,
                'party': 'Производство', 'note_number': None, 'unit_price': None,
                'notes': f'изтрита задача - {job.quantity} бр. "{job.detail.name}" ({reversed_by})',
            })
    movements.sort(key=lambda m: m['created_at'], reverse=True)

    return render_template('admin_material_history.html', material=material, movements=movements,
                            active_page='storage')


@app.route('/admin/details')
@login_required
def admin_details():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    details = Detail.query.order_by(Detail.name).all()
    services = Service.query.order_by(Service.name).all()
    services_data = [{'id': s.id, 'name': s.name, 'price_per_hour_eur': s.price_per_hour_eur} for s in services]
    return render_template('admin_details.html', materials=materials, details=details, services=services,
                            services_data=services_data, active_page='admin_details')


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


@app.route('/storage')
@role_required(['admin', 'worker'])
def storage_dashboard():
    """
    Read-only inventory hub (Склад): counts + links into the 3 stock views
    below, same hub/sub-page pattern as admin_dashboard(). Restocking itself
    happens on admin_delivery_notes(), not here - each sub-page just links
    there via a "Бърза заявка" button.
    """
    counts = {
        'materials': MaterialPrice.query.count(),
        'details': Detail.query.count(),
        'products': Product.query.count(),
    }
    request_counts = {
        status: MaterialRequest.query.filter_by(status=status).count() for status in REQUEST_STATUS_LABELS
    }
    return render_template('storage_dashboard.html', counts=counts, request_counts=request_counts, active_page='storage')


@app.route('/storage/materials')
@role_required(['admin', 'worker'])
def storage_materials():
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    # Group price-lots of "the same" material (name + dims + type - the same
    # fields _find_or_create_delivery_target matches on minus price/notes)
    # into one expandable row instead of a flat list of near-duplicates - see
    # CLAUDE.md delivery-note task. A group of size 1 renders exactly like a
    # plain row (no expand affordance, no aggregate fields computed); order
    # preserved from the query above.
    groups_by_key = {}
    material_groups = []
    for material in materials:
        key = (material.display_name, material.sheet_width_mm, material.sheet_length_mm,
               material.thickness_mm, material.height_mm, material.type)
        group = groups_by_key.get(key)
        if group is None:
            group = {'lots': []}
            groups_by_key[key] = group
            material_groups.append(group)
        group['lots'].append(material)

    for group in material_groups:
        lots = group['lots']
        if len(lots) < 2:
            continue
        first = lots[0]
        group['label'] = f"{first.display_name} ({_format_material_dims(first)})"
        group['unit'] = 'м' if first.type in ('rods', 'pipes', 'profiles') else 'м²'
        group['total_stock'] = sum(lot.stock_quantity for lot in lots)
        group['total_value'] = sum(lot.cost_per_m2 * _material_available_qty(lot) for lot in lots)
        group['min_price'] = min(lot.cost_per_m2 for lot in lots)
        group['max_price'] = max(lot.cost_per_m2 for lot in lots)
        # Worst case across lots, same red/yellow thresholds a plain row uses.
        if any(lot.stock_quantity <= 0 for lot in lots):
            group['row_bg'] = 'rgba(220, 53, 69, 0.12)'
        elif any(lot.min_quantity is not None and lot.stock_quantity <= lot.min_quantity for lot in lots):
            group['row_bg'] = 'rgba(255, 193, 7, 0.12)'
        else:
            group['row_bg'] = None

    return render_template('storage_materials.html', material_groups=material_groups, active_page='storage')


@app.route('/storage/requests')
@role_required(['admin', 'worker'])
def storage_requests():
    requests_list = MaterialRequest.query.order_by(MaterialRequest.created_at.desc()).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    return render_template(
        'storage_requests.html', requests=requests_list, suppliers=suppliers, materials=materials,
        status_labels=REQUEST_STATUS_LABELS, active_page='storage'
    )


@app.route('/storage/requests/create', methods=['POST'])
@role_required(['admin', 'worker'])
def create_material_request():
    """
    Creates one MaterialRequest per line in items_json ([{material_id, quantity}]) -
    the "new request" page lets an admin/worker queue up several materials at
    once instead of submitting one at a time. Invalid lines (unknown material,
    missing/non-positive quantity) are silently skipped rather than failing
    the whole batch, same tolerance as create_delivery_note()'s item loop.
    """
    try:
        items = json.loads(request.form.get('items_json', ''))
        if not isinstance(items, list):
            items = []
    except (TypeError, ValueError):
        items = []

    created_names = []
    for row in items:
        if not isinstance(row, dict):
            continue
        material = MaterialPrice.query.get(row.get('material_id'))
        quantity = row.get('quantity')
        if not material or not isinstance(quantity, (int, float)) or quantity <= 0:
            continue
        db.session.add(MaterialRequest(material_id=material.id, quantity=quantity, created_by_id=current_user.id))
        created_names.append(material.display_name)

    if not created_names:
        flash('Моля добавете поне един материал с валидно количество.', 'danger')
        return redirect(url_for('storage_requests'))

    db.session.commit()
    log_action(f'Създадени {len(created_names)} заявки за материали: {", ".join(created_names)}')
    flash(f'Добавени бяха {len(created_names)} заявки за материали.', 'success')
    return redirect(url_for('storage_requests'))


@app.route('/storage/requests/<int:request_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def update_material_request(request_id):
    material_request = MaterialRequest.query.get_or_404(request_id)
    status = request.form.get('status', '')
    if status not in REQUEST_STATUS_LABELS:
        flash('Невалиден статус.', 'danger')
        return redirect(url_for('storage_requests'))
    supplier_id = request.form.get('supplier_id', type=int)
    if status == 'ordered' and not supplier_id:
        flash('Изберете от кого е поръчан материалът, за да отбележите заявката като поръчана.', 'danger')
        return redirect(url_for('storage_requests'))
    material_request.status = status
    material_request.supplier_id = supplier_id if status == 'ordered' else None
    db.session.commit()
    log_action(f'Обновен статус на заявка #{material_request.id} на "{material_request.status_label}"')
    flash('Заявката беше обновена.', 'success')
    return redirect(url_for('storage_requests'))


@app.route('/storage/details')
@role_required(['admin', 'worker'])
def storage_details():
    details = Detail.query.order_by(Detail.name).all()
    return render_template('storage_details.html', details=details, active_page='storage')


@app.route('/storage/products')
@role_required(['admin', 'worker'])
def storage_products():
    products = Product.query.order_by(Product.name).all()
    return render_template('storage_products.html', products=products, active_page='storage')


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
    log_action(f'Създаден клиент "{name}"')
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
    log_action(describe_changes(f'клиент "{client.name}"', client, {
        'name': 'име', 'client_type': 'тип', 'email': 'имейл', 'phone': 'телефон',
        'eik': 'ЕИК', 'vat_number': 'ДДС №', 'address': 'адрес', 'mol': 'МОЛ',
    }))
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
    log_action(f'Изтрит клиент "{client.name}"')
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
    log_action(f'Създаден куриер "{name}"')
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
    log_action(describe_changes(f'куриер "{deliverer.name}"', deliverer, {
        'name': 'име', 'email': 'имейл', 'phone': 'телефон',
        'eik': 'ЕИК', 'vat_number': 'ДДС №', 'address': 'адрес', 'mol': 'МОЛ',
    }))
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
    log_action(f'Изтрит куриер "{deliverer.name}"')
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
                                     cost_per_m2=None, cutting_speed_mm_per_min=None, pierce_rate_per_min=None, components=None,
                                     material_type='sheets', notes=None):
    """
    Resolves one delivery-note line to a catalog row: reuses an existing
    Material/Detail/Product only if every descriptive field matches exactly,
    otherwise creates a brand-new bare-bones row (no DXF geometry / BOM -
    just what the paper delivery note itself carries). Two lines that differ
    in name, a dimension, price, or description (notes) must never be folded
    into the same stock count, per the "keep separate items separate" rule
    (see CLAUDE.md delivery-note task). brand and quantity do NOT split a
    row anymore - boss revised the rule: a different manufacturer/supplier
    tag or a different delivered quantity on an otherwise-identical material
    is still the same catalog item.

    For a material restock line specifically, this means several rows can
    legitimately share identical name/dims/type/notes and differ ONLY by
    price (that's the whole point of a price-lot). A fields-only lookup
    can't tell those apart, so when the caller supplies material_key (the
    admin picked a specific row from the delivery-note dropdown, rather than
    typing one in via "+ Нов материал..."), that row is resolved directly by
    key first and used as-is whenever this line's own fields still match it -
    never re-derived from a fields-only query that could land on a different
    lookalike lot (the lowest-id one) instead of the one actually selected.

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

    A *new* material must come with real cost_per_m2/cutting_speed_mm_per_min/
    pierce_rate_per_min (mirrors admin_add_material's required fields) - without
    them we'd otherwise silently create a zero-priced row that produces
    €0.00 CNC prices everywhere it's later picked. Returns None (skip the
    line) rather than defaulting to 0.0. Matching an *existing* material at
    the SAME price (or with no price entered on this line - the common case
    for a routine restock) reuses that row as-is. A different price on this
    line is a distinct batch/lot (the "keep separate items separate" rule
    applies to price too) - it clones the matched row's brand/dims/type/
    cutting-speed/pierce-rate into a brand-new row instead, so the two price
    lots keep separate stock counts. This is what lets the production wizard
    (admin_production_orders()) offer a choice between price lots of what is
    otherwise "the same" material.

    unit_price on a material line is the price of the whole unit received
    (a full sheet, one rod, etc.) - what a supplier's invoice actually lists
    - not an already-computed €/m² rate. _cost_per_m2_from_unit_price()
    derives cost_per_m2 from it using this line's own width/height, and the
    raw unit_price itself is kept as the new row's price_per_unit (same
    "singular price" column admin_materials.html edits) - see MaterialPrice.
    """
    name = (name or '').strip()
    brand = (brand or '').strip() or None
    notes = (notes or '').strip() or None
    price_per_unit = None

    if item_type == 'material':
        material_type = material_type if material_type in MATERIAL_TYPE_LABELS else 'sheets'
        # A restock line from the delivery-note dropdown carries material_key -
        # the exact row the admin picked - so trust that directly instead of
        # re-deriving identity from name/dims/type/notes: several price-lots
        # of the same material share every one of those fields by design, so
        # a fields-only lookup can't tell them apart and silently bumps
        # whichever lot happens to have the lowest id (see CLAUDE.md
        # delivery-note task - this used to bump the wrong lot entirely).
        # Only fall through to the fields-only lookup below when there's no
        # material_key (the "+ Нов материал..." flow, where the only goal is
        # avoiding an accidental duplicate of an already-typed-in material) or
        # when the line's own fields no longer match that exact row (the
        # admin edited dims/description away from the catalog default on this
        # line - see the "distinct variant row" fallback further down).
        existing = None
        if material_key:
            picked = MaterialPrice.query.filter_by(key=material_key).first()
            if picked and (picked.display_name, picked.notes, picked.sheet_width_mm,
                            picked.sheet_length_mm, picked.thickness_mm, picked.type) == \
                          (name, notes, width, height, thickness, material_type):
                existing = picked
        if existing is None and not material_key:
            existing = MaterialPrice.query.filter_by(
                display_name=name, notes=notes, sheet_width_mm=width,
                sheet_length_mm=height, thickness_mm=thickness, type=material_type
            ).first()
        if existing and unit_price is not None:
            derived_cost_per_m2 = _cost_per_m2_from_unit_price(material_type, unit_price, width, height)
            effective_cost_per_m2 = derived_cost_per_m2 if derived_cost_per_m2 is not None else unit_price
            if abs(existing.cost_per_m2 - effective_cost_per_m2) > 0.001:
                price_per_unit = unit_price
                cost_per_m2 = effective_cost_per_m2
                cutting_speed_mm_per_min = existing.cutting_speed_mm_per_min
                pierce_rate_per_min = existing.pierce_rate_per_min
                existing = None
        if existing:
            return existing
        # A line for an EXISTING catalog material whose dims/description were
        # edited on the delivery note (the real delivered batch differs
        # slightly from the catalog default - see DeliveryNoteItem docstring)
        # carries no pricing of its own from the form; the pricing inputs
        # only ever appear for the explicit "+ Нов материал..." flow. Without
        # this fallback the line would be silently dropped instead of
        # creating the distinct variant row the "keep separate items
        # separate" rule above calls for. material_key here identifies the
        # material the admin actually picked before editing its dimensions.
        if cost_per_m2 is None and material_key:
            source = MaterialPrice.query.filter_by(key=material_key).first()
            if source:
                cost_per_m2 = source.cost_per_m2
                cutting_speed_mm_per_min = source.cutting_speed_mm_per_min
                pierce_rate_per_min = source.pierce_rate_per_min
        # Rods are cut to length on a saw, never pierced or DXF-cut - no
        # cutting/drill speed required for them.
        if cost_per_m2 is None or (material_type != 'rods' and (cutting_speed_mm_per_min is None or pierce_rate_per_min is None)):
            return None
        if material_type == 'rods':
            cutting_speed_mm_per_min = None
            pierce_rate_per_min = None
        new_row = MaterialPrice(
            key='pending', display_name=name, cost_per_m2=cost_per_m2, price_per_unit=price_per_unit,
            cutting_speed_mm_per_min=cutting_speed_mm_per_min,
            pierce_rate_per_min=pierce_rate_per_min, sheet_width_mm=width, sheet_length_mm=height,
            thickness_mm=thickness, brand=brand, notes=notes, type=material_type, erp_number=_next_erp_number(),
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
    materials_data = [{'key': m.key, 'name': m.display_name, 'width': m.sheet_width_mm, 'height': m.sheet_length_mm,
                        'thickness': m.thickness_mm, 'brand': m.brand, 'price': None, 'type': m.type} for m in materials]
    details_data = [{'name': d.name, 'width': d.width, 'height': d.height,
                      'thickness': d.material.thickness_mm if d.material else None,
                      'brand': d.material.brand if d.material else None,
                      'material_key': d.material_key, 'price': d.calculated_price} for d in details]
    products_data = [{'name': p.name, 'width': None, 'height': None, 'thickness': None, 'brand': None, 'price': None} for p in products]
    services = Service.query.order_by(Service.name).all()
    return render_template(
        'admin_delivery_notes.html', suppliers=suppliers, notes=notes,
        materials=materials, details=details, products=products, services=services,
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
    log_action(f'Създаден доставчик "{name}"')
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
    brand, notes, cost_per_m2, cutting_speed_mm_per_min, pierce_rate_per_min,
    material_type, components}] - material_type (sheets/rods/profiles/pipes/
    other) only matters for a brand-new material line; defaults to 'sheets'
    if missing, same as _parse_material_type(). No id, since the row to
    bump/create is resolved from
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

    item_type_labels = {'material': 'материал', 'detail': 'детайл', 'product': 'продукт'}
    added_any = False
    log_parts = []
    log_detail_lines = []
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
        notes = (row.get('notes') or '').strip() or None
        material_key = (row.get('material_key') or '').strip() or None
        cost_per_m2 = _optional_float('cost_per_m2')
        cutting_speed_mm_per_min = _optional_float('cutting_speed_mm_per_min')
        pierce_rate_per_min = _optional_float('pierce_rate_per_min')
        material_type = (row.get('material_type') or 'sheets').strip()

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
            cost_per_m2=cost_per_m2, cutting_speed_mm_per_min=cutting_speed_mm_per_min, pierce_rate_per_min=pierce_rate_per_min,
            components=components, material_type=material_type, notes=notes
        )
        if not target:
            continue

        description = target.display_name if item_type == 'material' else target.name
        db.session.add(DeliveryNoteItem(
            delivery_note_id=note.id, target_type=item_type, target_id=target.id,
            description_snapshot=description, quantity=quantity, unit_price=unit_price,
            notes=notes,
            width=width, height=height, thickness=thickness, brand=brand,
        ))
        _bump_stock(target, quantity)
        added_any = True
        log_parts.append(f'{item_type_labels.get(item_type, item_type)} "{description}" +{quantity:g} (нова наличност {target.stock_quantity:g})')
        price_note = f', цена {unit_price:g} лв.' if unit_price is not None else ''
        notes_note = f', бележка: {row.get("notes")}' if (row.get('notes') or '').strip() else ''
        log_detail_lines.append(f'{item_type_labels.get(item_type, item_type)} "{description}": +{quantity:g} бр. → нова наличност {target.stock_quantity:g}{price_note}{notes_note}')

    if not added_any:
        db.session.rollback()
        flash('Няма валидни артикули за добавяне.', 'danger')
        return redirect(url_for('admin_delivery_notes'))

    db.session.commit()
    supplier_name = note.supplier.name if note.supplier else '-'
    header = f'Стокова разписка №{note.id} (доставчик: {supplier_name}, № {note.note_number or "-"}, дата {note.note_date or "-"})'
    log_action(f'Стокова разписка №{note.id}: ' + ', '.join(log_parts), details=header + '\n' + '\n'.join(log_detail_lines))
    flash('Стоковата разписка беше записана и наличностите бяха обновени.', 'success')
    return redirect(url_for('admin_delivery_notes'))


@app.route('/admin/delivery-notes/<int:note_id>/print')
@role_required(['admin', 'worker'])
def admin_delivery_note_print(note_id):
    """
    Browser-print view of a DeliveryNote (стокова разписка) - same
    @media-print pattern as admin_offer_print.html/offer.html (see CLAUDE.md's
    'Offer / protocol / certificate documents' section), reused for a
    goods-received note instead of a sales quote.
    """
    note = DeliveryNote.query.get_or_404(note_id)
    return render_template('admin_delivery_note_print.html', note=note)


@app.route('/admin/client-delivery-notes')
@role_required(['admin', 'worker'])
def admin_client_delivery_notes():
    """
    Intake page for the reverse flow of admin_delivery_notes(): issuing stock
    to a Client instead of receiving it from a Supplier. Every line must pick
    an existing catalog row (materials_data/details_data/products_data carry
    `id`, not a `key`/`name` to resolve on submit) - unlike the supplier
    intake form there's no "+ Нов ..." option, since there's nothing to
    create when stock is only leaving.
    """
    clients = Client.query.order_by(Client.name).all()
    notes = ClientDeliveryNote.query.order_by(ClientDeliveryNote.created_at.desc()).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    details = Detail.query.order_by(Detail.name).all()
    products = Product.query.order_by(Product.name).all()
    materials_data = [{'id': m.id, 'name': format_material_option(m), 'price': None, 'stock': m.stock_quantity} for m in materials]
    details_data = [{'id': d.id, 'name': d.name, 'price': d.total_price, 'stock': d.stock_quantity} for d in details]
    products_data = [{'id': p.id, 'name': p.name, 'price': None, 'stock': p.stock_quantity} for p in products]
    return render_template(
        'admin_client_delivery_notes.html', clients=clients, notes=notes, materials=materials,
        materials_data=materials_data, details_data=details_data, products_data=products_data,
        active_page='admin_client_delivery_notes'
    )


@app.route('/admin/client-delivery-notes/create', methods=['POST'])
@role_required(['admin', 'worker'])
def create_client_delivery_note():
    """
    Records a client delivery note and decrements stock_quantity on every
    referenced Material/Detail/Product - the mirror of create_delivery_note().
    items_json follows [{type, target_id, qty, unit_price, notes}] - target_id
    is required and must resolve to an existing row (see
    admin_client_delivery_notes()'s docstring for why, unlike
    create_delivery_note() there's no _find_or_create_delivery_target here).
    """
    try:
        items = json.loads(request.form.get('items_json', ''))
        if not isinstance(items, list):
            items = []
    except (TypeError, ValueError):
        items = []

    if not items:
        flash('Моля добавете поне един артикул към стоковата разписка.', 'danger')
        return redirect(url_for('admin_client_delivery_notes'))

    client_id_raw = request.form.get('client_id', '')
    client_id = int(client_id_raw) if client_id_raw and client_id_raw.isdigit() else None
    note_date_raw = request.form.get('note_date', '').strip()
    note_date = datetime.strptime(note_date_raw, '%Y-%m-%d').date() if note_date_raw else None

    note = ClientDeliveryNote(
        client_id=client_id,
        note_number=request.form.get('note_number', '').strip() or None,
        note_date=note_date,
        created_by_id=current_user.id,
    )
    db.session.add(note)
    db.session.flush()

    item_type_labels = {'material': 'материал', 'detail': 'детайл', 'product': 'продукт'}
    added_any = False
    log_parts = []
    log_detail_lines = []
    for row in items:
        if not isinstance(row, dict):
            continue
        item_type = row.get('type')
        model = DELIVERY_NOTE_TARGET_MODELS.get(item_type)
        if not model:
            continue
        try:
            target_id = int(row.get('target_id'))
            quantity = float(row.get('qty', 0))
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        target = model.query.get(target_id)
        if not target:
            continue

        try:
            unit_price_raw = row.get('unit_price')
            unit_price = float(unit_price_raw) if unit_price_raw not in (None, '') else None
        except (TypeError, ValueError):
            unit_price = None

        description = target.display_name if item_type == 'material' else target.name
        db.session.add(ClientDeliveryNoteItem(
            client_delivery_note_id=note.id, target_type=item_type, target_id=target.id,
            description_snapshot=description, quantity=quantity, unit_price=unit_price,
            notes=(row.get('notes') or '').strip() or None,
        ))
        _bump_stock(target, -quantity)
        added_any = True
        log_parts.append(f'{item_type_labels.get(item_type, item_type)} "{description}" -{quantity:g} (нова наличност {target.stock_quantity:g})')
        price_note = f', цена {unit_price:g} €' if unit_price is not None else ''
        notes_note = f', бележка: {row.get("notes")}' if (row.get('notes') or '').strip() else ''
        log_detail_lines.append(f'{item_type_labels.get(item_type, item_type)} "{description}": -{quantity:g} бр. → нова наличност {target.stock_quantity:g}{price_note}{notes_note}')

    if not added_any:
        db.session.rollback()
        flash('Няма валидни артикули за добавяне.', 'danger')
        return redirect(url_for('admin_client_delivery_notes'))

    db.session.commit()
    client_name = note.client.name if note.client else '-'
    header = f'Стокова разписка (издадена) №{note.id} (клиент: {client_name}, № {note.note_number or "-"}, дата {note.note_date or "-"})'
    log_action(f'Издадена стокова разписка №{note.id}: ' + ', '.join(log_parts), details=header + '\n' + '\n'.join(log_detail_lines))
    flash('Стоковата разписка беше записана и наличностите бяха обновени.', 'success')
    return redirect(url_for('admin_client_delivery_notes'))


@app.route('/admin/client-delivery-notes/<int:note_id>/print')
@role_required(['admin', 'worker'])
def admin_client_delivery_note_print(note_id):
    """Browser-print view of a ClientDeliveryNote - mirrors admin_delivery_note_print()."""
    note = ClientDeliveryNote.query.get_or_404(note_id)
    return render_template('admin_client_delivery_note_print.html', note=note)


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


IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
# Only filenames our own upload code produces look like this - the seeded
# cards' images (e.g. "dmg-mori-dmu75.jpg") never do, and several seeded
# cards on different pages deliberately share the same committed image file,
# so only a file matching this prefix is ever safe to delete from disk.
_UPLOADED_IMAGE_PREFIX_RE = re.compile(r'^[0-9a-f]{32}_')


def _save_upload(file, folder, allowed_extensions=None):
    """Saves an uploaded file into folder with a collision-safe uuid-prefixed
    filename; returns the saved filename, or None if no file was submitted
    (or, when allowed_extensions is given, its extension isn't in it)."""
    if not file or file.filename == '':
        return None
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if allowed_extensions is not None and ext not in allowed_extensions:
        return None
    unique_filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    file.save(os.path.join(folder, unique_filename))
    return unique_filename


@app.route('/services/machine-cards/new')
@login_required
def new_machine_card_window():
    """Popup 'add new machine/product card' window - opened from services.html or index.html."""
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    page = 'index' if request.args.get('page') == 'index' else 'services'
    kind = 'product' if request.args.get('kind') == 'product' else 'machine'
    noun = 'продукта' if kind == 'product' else 'машината'
    fields = [
        {'name': 'series_label', 'label': 'Кратък етикет (напр. 5-ОСНО ФРЕЗОВАНЕ)', 'value': '', 'type': 'text'},
        {'name': 'title', 'label': f'Име на {noun}', 'value': '', 'type': 'text', 'required': True},
        {'name': 'image', 'label': f'Снимка на {noun} (по избор)', 'value': '', 'type': 'file'},
    ]
    if page == 'services' and kind == 'machine':
        # Only the services page's machines are grouped by section - pick an
        # existing section (e.g. "ФРЕЗОВИ ЦЕНТРОВЕ") or type a brand-new one.
        # Products are always a flat grid, so they skip this field.
        fields.append({'name': 'section_title', 'label': 'Раздел (напр. фрезоване, струговане, листообработка)',
                        'value': '', 'type': 'datalist', 'options': _service_section_titles()})
    fields += [
        {'name': 'specs_text', 'label': 'Характеристики (незадължително, по един ред "Етикет: Стойност")', 'value': '', 'type': 'textarea'},
        {'name': 'description', 'label': 'Описание', 'value': '', 'type': 'textarea'},
    ]
    return render_template(
        'edit_window.html', item_label='нов продукт' if kind == 'product' else 'нова машина',
        saved=request.args.get('saved') == '1',
        action=url_for('create_machine_card', page=page, kind=kind), fields=fields
    )


@app.route('/services/machine-cards/create', methods=['POST'])
@login_required
def create_machine_card():
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    page = 'index' if request.args.get('page') == 'index' else 'services'
    kind = 'product' if request.args.get('kind') == 'product' else 'machine'
    redirect_target = _machine_card_home(page)

    title = request.form.get('title', '').strip()
    if not title:
        flash('Моля въведете име.', 'danger')
        return redirect(redirect_target)

    card = ServiceMachineCard(
        page=page,
        kind=kind,
        title=title,
        series_label=request.form.get('series_label', '').strip() or None,
        section_title=request.form.get('section_title', '').strip() or None if page == 'services' and kind == 'machine' else None,
        specs_text=request.form.get('specs_text', '').strip() or None,
        description=request.form.get('description', '').strip() or None,
        image_filename=_save_upload(request.files.get('image'), app.config['MACHINE_IMAGES_FOLDER'], IMAGE_EXTENSIONS),
    )
    db.session.add(card)
    db.session.commit()
    log_action(f'Създадена карта "{title}" ({"продукт" if kind == "product" else "машина"}, страница {page})')
    flash('Добавено успешно.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('new_machine_card_window', page=page, kind=kind, saved='1'))
    return redirect(redirect_target)


@app.route('/services/machine-cards/<int:card_id>/edit')
@login_required
def edit_machine_card_window(card_id):
    if not current_user.can_edit_content:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    card = ServiceMachineCard.query.get_or_404(card_id)
    noun = 'продукта' if card.kind == 'product' else 'машината'
    fields = [
        {'name': 'series_label', 'label': 'Кратък етикет (напр. 5-ОСНО ФРЕЗОВАНЕ)', 'value': card.series_label or '', 'type': 'text'},
        {'name': 'title', 'label': f'Име на {noun}', 'value': card.title, 'type': 'text', 'required': True},
        {'name': 'image', 'label': f'Снимка на {noun} (по избор - оставете празно, за да запазите текущата)', 'value': '', 'type': 'file',
         'preview_url': url_for('static', filename='img/machines/' + card.image_filename) if card.image_filename else None},
    ]
    if card.page == 'services' and card.kind == 'machine':
        fields.append({'name': 'section_title', 'label': 'Раздел (напр. фрезоване, струговане, листообработка)',
                        'value': card.section_title or '', 'type': 'datalist', 'options': _service_section_titles()})
    fields += [
        {'name': 'specs_text', 'label': 'Характеристики (незадължително, по един ред "Етикет: Стойност")', 'value': card.specs_text or '', 'type': 'textarea'},
        {'name': 'description', 'label': 'Описание', 'value': card.description or '', 'type': 'textarea'},
    ]
    return render_template(
        'edit_window.html', item_label='продукт' if card.kind == 'product' else 'машина',
        saved=request.args.get('saved') == '1',
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
        flash('Моля въведете име.', 'danger')
        return redirect(redirect_target)

    card.title = title
    card.series_label = request.form.get('series_label', '').strip() or None
    if card.page == 'services' and card.kind == 'machine':
        card.section_title = request.form.get('section_title', '').strip() or None
    card.specs_text = request.form.get('specs_text', '').strip() or None
    card.description = request.form.get('description', '').strip() or None

    new_image_filename = _save_upload(request.files.get('image'), app.config['MACHINE_IMAGES_FOLDER'], IMAGE_EXTENSIONS)
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

    log_action(describe_changes(f'карта "{card.title}"', card, {
        'title': 'име', 'series_label': 'етикет', 'section_title': 'раздел',
        'specs_text': 'характеристики', 'description': 'описание', 'image_filename': 'снимка',
    }))
    db.session.commit()
    flash('Обновено успешно.', 'success')
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
    title = card.title
    db.session.delete(card)
    db.session.commit()
    log_action(f'Изтрита карта "{title}"')
    flash('Премахнато успешно.', 'success')
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
    old_content = row.content if row else None
    if row:
        row.content = content
    else:
        db.session.add(EditableText(key=key, content=content))
    db.session.commit()
    # Free text can be long - show only a short before/after preview, not the full block.
    old_preview = (old_content[:60] + '…') if old_content and len(old_content) > 60 else (old_content or '(по подразбиране)')
    new_preview = (content[:60] + '…') if len(content) > 60 else content
    log_action(f'Редактиран текст "{key}": "{old_preview}" → "{new_preview}"')
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
    if role not in ('regular_user', 'worker', 'admin', 'web_designer', 'quality_control'):
        flash('Невалидна роля.', 'danger')
        return redirect(url_for('admin_users'))

    secure_pass = generate_password_hash(password, method='scrypt')
    new_user = User(username=username, password=secure_pass, role=role)
    db.session.add(new_user)
    db.session.commit()
    log_action(f'Създаден потребител "{username}" (роля {role})')
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
    if role not in ('regular_user', 'worker', 'admin', 'web_designer', 'quality_control'):
        flash('Невалидна роля.', 'danger')
        return redirect(url_for('admin_users'))

    user_to_update = User.query.get_or_404(user_id)
    old_role = user_to_update.role
    user_to_update.role = role
    db.session.commit()
    log_action(f'Роля на "{user_to_update.username}": {old_role} → {role}')
    flash(f'Ролята на {user_to_update.username} беше обновена успешно.', 'success')
    return redirect(url_for('admin_users'))


def _parse_optional_float(form, field):
    """Reads one optional numeric form field (blank -> None). Raises
    ValueError on non-numeric or negative input, same as the required cost
    fields, so callers can catch it in one place alongside those."""
    raw = form.get(field, '').strip()
    value = float(raw) if raw else None
    if value is not None and value < 0:
        raise ValueError(f'{field} must not be negative')
    return value


def _parse_sheet_dimensions(form):
    """
    Reads the optional sheet_length_mm/sheet_width_mm/thickness_mm/height_mm
    fields. All four are optional (blank -> None) since not every material
    entry represents a specific stock size, and height_mm only applies to
    'profiles' (see MaterialPrice.height_mm). Raises ValueError on
    non-numeric input, same as the required cost fields, so callers can catch
    it in one place.
    """
    return [_parse_optional_float(form, field) for field in
            ('sheet_length_mm', 'sheet_width_mm', 'thickness_mm', 'height_mm')]


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


def _material_variant_exists(display_name, material_type, brand, cost_per_m2, cutting_speed_mm_per_min,
                              pierce_rate_per_min, sheet_length_mm, sheet_width_mm, thickness_mm, height_mm):
    """
    Two materials only count as the same catalog row if every property
    matches - name/brand alone are not enough. This lets e.g. a 2mm and a
    3mm sheet of the same aluminum (same name, same brand) both get created
    as distinct MaterialPrice rows; only a byte-for-byte resubmit (double
    click) is rejected as a duplicate.
    """
    return MaterialPrice.query.filter_by(
        display_name=display_name, type=material_type, brand=brand, cost_per_m2=cost_per_m2,
        cutting_speed_mm_per_min=cutting_speed_mm_per_min, pierce_rate_per_min=pierce_rate_per_min,
        sheet_length_mm=sheet_length_mm, sheet_width_mm=sheet_width_mm, thickness_mm=thickness_mm,
        height_mm=height_mm
    ).first() is not None


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
    material_type = _parse_material_type(request.form)

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        # Rods/profiles are cut to length on a saw, never pierced or DXF-cut
        # - no cutting/drill speed for either.
        skip_speed_fields = material_type in ('rods', 'profiles')
        cutting_speed_mm_per_min = None if skip_speed_fields else float(request.form.get('cutting_speed_mm_per_min', ''))
        # Entered as seconds per pierce (shop-floor friendly), stored as the
        # pierces/min rate the pricing formula (_service_time_cost) uses.
        pierce_time_sec = None if skip_speed_fields else float(request.form.get('pierce_time_sec', ''))
        sheet_length_mm, sheet_width_mm, thickness_mm, height_mm = _parse_sheet_dimensions(request.form)
        price_per_kg_m2 = _parse_optional_float(request.form, 'price_per_kg_m2')
        price_per_kg_m = _parse_optional_float(request.form, 'price_per_kg_m')
        weight_kg = _parse_optional_float(request.form, 'weight_kg')
        price_per_unit = _parse_optional_float(request.form, 'price_per_unit')
        min_quantity = _parse_optional_float(request.form, 'min_quantity')
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        flash('Всички цени, размери и ERP № трябва да бъдат валидни числа.', 'danger')
        return redirect(url_for('admin_materials'))

    if cost_per_m2 < 0 or (cutting_speed_mm_per_min is not None and cutting_speed_mm_per_min <= 0) \
            or (pierce_time_sec is not None and pierce_time_sec <= 0) \
            or (min_quantity is not None and min_quantity < 0):
        flash('Цената не може да бъде отрицателна, а скоростта на рязане/времето за пробождане трябва да бъдат положителни числа.', 'danger')
        return redirect(url_for('admin_materials'))
    pierce_rate_per_min = 60.0 / pierce_time_sec if pierce_time_sec else None

    conflict = _erp_number_conflict(erp_number, exclude_type='material', exclude_id=material.id)
    if conflict:
        flash(f'ERP № {erp_number} вече се използва от {conflict}.', 'danger')
        return redirect(url_for('admin_materials'))

    # Round to 2 decimals - keeps prices in a simple, everyday currency
    # format rather than accumulating long float tails over repeated edits.
    material.cost_per_m2 = round(cost_per_m2, 2)
    material.cutting_speed_mm_per_min = round(cutting_speed_mm_per_min, 2) if cutting_speed_mm_per_min is not None else None
    material.pierce_rate_per_min = round(pierce_rate_per_min, 2) if pierce_rate_per_min is not None else None
    material.sheet_length_mm = sheet_length_mm
    material.sheet_width_mm = sheet_width_mm
    material.thickness_mm = thickness_mm
    material.height_mm = height_mm
    material.price_per_kg_m2 = round(price_per_kg_m2, 2) if price_per_kg_m2 is not None else None
    material.price_per_kg_m = round(price_per_kg_m, 2) if price_per_kg_m is not None else None
    material.weight_kg = round(weight_kg, 2) if weight_kg is not None else None
    material.price_per_unit = round(price_per_unit, 2) if price_per_unit is not None else None
    material.min_quantity = round(min_quantity, 2) if min_quantity is not None else None
    material.erp_number = erp_number
    material.code_number = request.form.get('code_number', '').strip() or None
    material.type = _parse_material_type(request.form)
    material.brand = request.form.get('brand', '').strip() or None
    material.notes = request.form.get('notes', '').strip() or None
    material.display_name_en = request.form.get('display_name_en', '').strip() or None
    material.display_name_de = request.form.get('display_name_de', '').strip() or None
    log_action(describe_changes(f'материал "{material.display_name}"', material, {
        'cost_per_m2': 'цена лв/м²', 'cutting_speed_mm_per_min': 'ск. рязане mm/min',
        'pierce_rate_per_min': 'пробождания/min', 'sheet_length_mm': 'дължинаmm',
        'sheet_width_mm': 'ширина mm', 'thickness_mm': 'дебелина mm', 'height_mm': 'височина mm',
        'price_per_kg_m2': 'цена лв/кг(м²)', 'price_per_kg_m': 'цена лв/кг(м)', 'weight_kg': 'тегло кг',
        'price_per_unit': 'цена за цяло', 'min_quantity': 'мин. количество', 'erp_number': 'ERP №',
        'code_number': 'КД №', 'type': 'тип', 'brand': 'марка', 'notes': 'забележка',
    }))
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

    material_type = _parse_material_type(request.form)
    brand = request.form.get('brand', '').strip() or None

    # Rods/profiles are cut to length on a saw, never pierced or DXF-cut -
    # no cutting/drill speed for either.
    skip_speed_fields = material_type in ('rods', 'profiles')

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        cutting_speed_mm_per_min = None if skip_speed_fields else float(request.form.get('cutting_speed_mm_per_min', ''))
        # Entered as seconds per pierce (shop-floor friendly), stored as the
        # pierces/min rate the pricing formula (_service_time_cost) uses.
        pierce_time_sec = None if skip_speed_fields else float(request.form.get('pierce_time_sec', ''))
        sheet_length_mm, sheet_width_mm, thickness_mm, height_mm = _parse_sheet_dimensions(request.form)
        price_per_kg_m2 = _parse_optional_float(request.form, 'price_per_kg_m2')
        price_per_kg_m = _parse_optional_float(request.form, 'price_per_kg_m')
        weight_kg = _parse_optional_float(request.form, 'weight_kg')
        price_per_unit = _parse_optional_float(request.form, 'price_per_unit')
        min_quantity = _parse_optional_float(request.form, 'min_quantity')
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        flash('Всички цени, размери и ERP № трябва да бъдат валидни числа.', 'danger')
        return redirect(url_for('admin_materials'))

    if cost_per_m2 < 0 or (cutting_speed_mm_per_min is not None and cutting_speed_mm_per_min <= 0) \
            or (pierce_time_sec is not None and pierce_time_sec <= 0) \
            or (min_quantity is not None and min_quantity < 0):
        flash('Цената не може да бъде отрицателна, а скоростта на рязане/времето за пробождане трябва да бъдат положителни числа.', 'danger')
        return redirect(url_for('admin_materials'))
    pierce_rate_per_min = 60.0 / pierce_time_sec if pierce_time_sec else None

    # Only a byte-for-byte resubmit (double click) is rejected - a difference
    # in any property (e.g. thickness) always makes a distinct catalog row,
    # even under the same name/brand.
    if _material_variant_exists(display_name, material_type, brand, round(cost_per_m2, 2),
                                 round(cutting_speed_mm_per_min, 2) if cutting_speed_mm_per_min is not None else None,
                                 round(pierce_rate_per_min, 2) if pierce_rate_per_min is not None else None,
                                 sheet_length_mm, sheet_width_mm, thickness_mm, height_mm):
        flash(f'Вече съществува идентичен материал с име "{display_name}".', 'danger')
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
        cutting_speed_mm_per_min=round(cutting_speed_mm_per_min, 2) if cutting_speed_mm_per_min is not None else None,
        pierce_rate_per_min=round(pierce_rate_per_min, 2) if pierce_rate_per_min is not None else None,
        sheet_length_mm=sheet_length_mm,
        sheet_width_mm=sheet_width_mm,
        thickness_mm=thickness_mm,
        height_mm=height_mm,
        price_per_kg_m2=round(price_per_kg_m2, 2) if price_per_kg_m2 is not None else None,
        price_per_kg_m=round(price_per_kg_m, 2) if price_per_kg_m is not None else None,
        weight_kg=round(weight_kg, 2) if weight_kg is not None else None,
        price_per_unit=round(price_per_unit, 2) if price_per_unit is not None else None,
        min_quantity=round(min_quantity, 2) if min_quantity is not None else None,
        erp_number=erp_number,
        code_number=request.form.get('code_number', '').strip() or None,
        type=material_type,
        brand=brand,
        notes=request.form.get('notes', '').strip() or None,
        display_name_en=request.form.get('display_name_en', '').strip() or None,
        display_name_de=request.form.get('display_name_de', '').strip() or None,
    )
    db.session.add(new_material)
    db.session.flush()  # assigns new_material.id without a full commit yet
    new_material.key = f'material_{new_material.id}'
    db.session.commit()

    log_action(f'Създаден материал "{display_name}" (цена {new_material.cost_per_m2:g} лв/м², тип {material_type})')
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

    for file in files:
        if file and file.filename != '':
            unique_filename = _save_upload(file, app.config['PRODUCT_IMAGES_FOLDER'], IMAGE_EXTENSIONS)
            if unique_filename:
                db.session.add(ProductImage(product_id=product.id, filename=unique_filename))
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

    new_machine = Machine(name=name, machine_type=request.form.get('machine_type', '').strip() or None)
    db.session.add(new_machine)
    db.session.commit()
    log_action(f'Създадена машина "{name}"')
    flash('Машината е добавена успешно!', 'success')
    return redirect(url_for('list_machines'))


def _known_machine_types():
    """Distinct non-blank Machine.machine_type values already in use, offered as
    datalist suggestions so admins reuse the same category string instead of
    accidentally typing a near-duplicate (e.g. 'laser' vs 'Laser')."""
    rows = db.session.query(Machine.machine_type).filter(Machine.machine_type.isnot(None)).distinct().all()
    return sorted({r[0] for r in rows if r[0]})


@app.route('/machines/<int:id>/edit')
@login_required
def edit_machine_window(id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /machines."""
    if not (current_user.is_admin or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    machine = Machine.query.get_or_404(id)
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    panels = ElectricalPanel.query.join(Room).order_by(Room.name, ElectricalPanel.name).all()
    return render_template(
        'edit_window.html', item_label='машина', saved=request.args.get('saved') == '1',
        action=url_for('rename_machine', id=machine.id),
        fields=[
            {'name': 'name', 'label': 'Име на машина', 'value': machine.name, 'type': 'text', 'required': True},
            {'name': 'machine_type', 'label': 'Тип машина (напр. laser, mill_3axis)',
             'value': machine.machine_type or '', 'type': 'datalist', 'options': _known_machine_types()},
            {'name': 'room_id', 'label': 'Помещение', 'value': machine.room_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': r.id, 'label': f'{r.building.name} / {r.name}'} for r in rooms]},
            {'name': 'panel_id', 'label': 'Свързана към ел. табло', 'value': machine.panel_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': p.id, 'label': f'{p.room.name} / {p.name}'} for p in panels]},
        ]
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
    machine.machine_type = request.form.get('machine_type', '').strip() or None
    room_id_raw = request.form.get('room_id', '')
    panel_id_raw = request.form.get('panel_id', '')
    machine.room_id = int(room_id_raw) if room_id_raw.isdigit() else None
    machine.panel_id = int(panel_id_raw) if panel_id_raw.isdigit() else None
    log_action(describe_changes(f'машина #{id}', machine, {'name': 'име', 'machine_type': 'тип'}))
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
    old_status = machine.status
    machine.status = status
    db.session.commit()
    log_action(f'Статус на машина "{machine.name}": {old_status} → {status}')
    flash(f'Статусът на {machine.name} е актуализиран.', 'success')
    return redirect(url_for('list_machines'))

@app.route('/machines')
@login_required
def list_machines():
    if not (current_user.is_staff or current_user.can_edit_content):
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    machines = Machine.query.all()
    return render_template('machines.html', machines=machines, known_machine_types=_known_machine_types(), active_page='machines')


# ----------------- УСЛУГИ (billable Services catalog) -----------------

@app.route('/admin/services')
@login_required
def admin_services():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))
    services = Service.query.order_by(Service.name).all()
    all_machines = Machine.query.order_by(Machine.name).all()
    return render_template('admin_services.html', services=services, known_machine_types=_known_machine_types(),
                            all_machines=all_machines, active_page='admin_services')


def _selected_machines(form):
    """Machine rows picked in a <select multiple name="machine_ids">, ignoring
    any unknown ids rather than raising - see admin_add_service()/
    admin_update_service(). See _parse_machine_ids() for the strict variant
    used by the power-device routes, which reject an unknown id instead."""
    ids = _parse_machine_ids(form)
    return Machine.query.filter(Machine.id.in_(ids)).all() if ids else []


def _parse_service_pricing(form):
    """Validates the price_per_hour_eur/pricing_mode/price_per_meter_eur trio
    shared by admin_add_service()/admin_update_service(). The hourly rate is
    always required (even for a length-priced service) so the base DXF cut
    pipeline - which only ever reads price_per_hour_eur, see
    calculate_cnc_price() - keeps pricing correctly if this Service is ever
    picked as a Detail's cutting service. Returns (price_per_hour_eur,
    pricing_mode, price_per_meter_eur, error_message); error_message is None
    on success."""
    try:
        price_per_hour_eur = float(form.get('price_per_hour_eur', ''))
    except ValueError:
        return None, None, None, 'Цената на час трябва да бъде валидно число.'
    if price_per_hour_eur <= 0:
        return None, None, None, 'Цената на час трябва да бъде положително число.'

    pricing_mode = form.get('pricing_mode', 'time').strip()
    if pricing_mode not in ('time', 'length'):
        pricing_mode = 'time'

    price_per_meter_eur = None
    if pricing_mode == 'length':
        try:
            price_per_meter_eur = float(form.get('price_per_meter_eur', ''))
        except ValueError:
            return None, None, None, 'Цената на метър рязане трябва да бъде валидно число.'
        if price_per_meter_eur <= 0:
            return None, None, None, 'Цената на метър рязане трябва да бъде положително число.'

    return round(price_per_hour_eur, 2), pricing_mode, price_per_meter_eur, None


@app.route('/admin/services/add', methods=['POST'])
@login_required
def admin_add_service():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на услугата.', 'danger')
        return redirect(url_for('admin_services'))

    price_per_hour_eur, pricing_mode, price_per_meter_eur, error = _parse_service_pricing(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_services'))

    new_service = Service(
        name=name,
        machine_type=request.form.get('machine_type', '').strip() or None,
        price_per_hour_eur=price_per_hour_eur,
        pricing_mode=pricing_mode,
        price_per_meter_eur=price_per_meter_eur,
        description=request.form.get('description', '').strip() or None,
        name_en=request.form.get('name_en', '').strip() or None,
        name_de=request.form.get('name_de', '').strip() or None,
        description_en=request.form.get('description_en', '').strip() or None,
        description_de=request.form.get('description_de', '').strip() or None,
        show_price='show_price' in request.form,
        machines=_selected_machines(request.form),
    )
    db.session.add(new_service)
    db.session.commit()
    log_action(f'Създадена услуга "{name}" ({price_per_hour_eur:g} €/час)')
    flash(f'Услугата "{name}" беше добавена успешно.', 'success')
    return redirect(url_for('admin_services'))


@app.route('/admin/services/<int:service_id>/update', methods=['POST'])
@login_required
def admin_update_service(service_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    service = Service.query.get_or_404(service_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на услугата.', 'danger')
        return redirect(url_for('admin_services'))

    price_per_hour_eur, pricing_mode, price_per_meter_eur, error = _parse_service_pricing(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_services'))

    service.name = name
    service.machine_type = request.form.get('machine_type', '').strip() or None
    service.price_per_hour_eur = price_per_hour_eur
    service.pricing_mode = pricing_mode
    service.price_per_meter_eur = price_per_meter_eur
    service.description = request.form.get('description', '').strip() or None
    service.name_en = request.form.get('name_en', '').strip() or None
    service.name_de = request.form.get('name_de', '').strip() or None
    service.description_en = request.form.get('description_en', '').strip() or None
    service.description_de = request.form.get('description_de', '').strip() or None
    service.show_price = 'show_price' in request.form
    service.machines = _selected_machines(request.form)
    log_action(describe_changes(f'услуга "{service.name}"', service, {
        'name': 'име', 'machine_type': 'тип машина', 'price_per_hour_eur': 'цена €/час',
        'pricing_mode': 'режим ценообр.', 'price_per_meter_eur': 'цена €/м', 'description': 'описание',
        'show_price': 'видима цена в /services',
    }))
    db.session.commit()
    flash(f'Услугата "{name}" беше обновена успешно.', 'success')
    return redirect(url_for('admin_services'))


@app.route('/admin/services/<int:service_id>/delete', methods=['POST'])
@login_required
def admin_delete_service(service_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    service = Service.query.get_or_404(service_id)
    # A service used by an existing cut/operation can't be deleted out from
    # under it - same "resolve usages first" rule as admin_delete_detail()
    # for a Detail still attached to a Product.
    in_use = (
        DxfFile.query.filter_by(service_id=service.id).first()
        or Detail.query.filter_by(cutting_service_id=service.id).first()
        or Operation.query.filter_by(service_id=service.id).first()
    )
    if in_use:
        flash(f'Услугата "{service.name}" се използва и не може да бъде изтрита.', 'danger')
        return redirect(url_for('admin_services'))

    name = service.name
    db.session.delete(service)
    db.session.commit()
    log_action(f'Изтрита услуга "{name}"')
    flash(f'Услугата "{service.name}" беше изтрита.', 'success')
    return redirect(url_for('admin_services'))


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
    service_id_raw = request.form.get('service_id', '')
    service_id = int(service_id_raw) if service_id_raw and service_id_raw.isdigit() else None
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

    # A DXF is optional - a detail can be catalogued with a manually-entered
    # price up front and have its DXF/geometry added later via
    # detail_dxf_dashboard(), same bare-bones shape _find_or_create_delivery_target
    # already creates for a delivery-note "new detail" line with no DXF.
    file = request.files.get('file')
    has_dxf = bool(file and file.filename)

    if has_dxf and not file.filename.lower().endswith('.dxf'):
        flash('Невалиден формат! Приемат се само .dxf файлове.', 'danger')
        return redirect(url_for('admin_details'))

    # Picking a cutting service is optional here - unlike calculated_price
    # (material only, see calculate_material_price()), cutting cost isn't
    # required to price a Detail at all. If a length-priced service IS
    # picked, _add_cutting_operation() auto-attaches a cutting Operation for
    # it below; anything else (blank, or a time-priced service) just gets
    # recorded as cutting_service_id without an auto-created Operation.
    if service_id and not db.session.get(Service, service_id):
        flash('Невалиден избор на услуга.', 'danger')
        return redirect(url_for('admin_details'))

    manual_price = None
    if not has_dxf:
        try:
            manual_price = float(request.form.get('manual_price', ''))
        except ValueError:
            flash('Моля качете .dxf файл или въведете цена на детайла ръчно.', 'danger')
            return redirect(url_for('admin_details'))
        if manual_price < 0:
            flash('Цената не може да бъде отрицателна.', 'danger')
            return redirect(url_for('admin_details'))

    pdf_file = request.files.get('pdf_file')
    if pdf_file and pdf_file.filename and not pdf_file.filename.lower().endswith('.pdf'):
        flash('Невалиден формат за референтен файл! Приемат се само .pdf файлове.', 'danger')
        return redirect(url_for('admin_details'))

    # Saved straight into DETAIL_DXF_FOLDER (not the scratch UPLOAD_FOLDER) so
    # the file survives past this request - see detail_dxf_dashboard() /
    # DetailDxfFile. Only removed again below if something fails.
    stored_path = None
    try:
        if has_dxf:
            # secure_filename() strips non-ASCII entirely (a Cyrillic-only
            # name like "панел_метален.dxf" becomes just "dxf", losing even
            # the extension) - fine for the on-disk name below, but the
            # DB's display name (original_filename, shown in the dashboard
            # and used to gate the .dxf preview button) needs
            # sanitize_display_filename() instead, which keeps Cyrillic.
            original_filename = sanitize_display_filename(file.filename)
            stored_filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
            stored_path = os.path.join(app.config['DETAIL_DXF_FOLDER'], stored_filename)
            file.save(stored_path)

            width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(stored_path)
            if width is None:
                flash('Грешка при обработката на DXF структурата.', 'danger')
                os.remove(stored_path)
                return redirect(url_for('admin_details'))

            price = calculate_material_price(width, height, material_key)
            new_detail = Detail(
                name=name, material_key=material_key, width=width, height=height,
                total_length=total_length, pierce_count=pierce_count,
                calculated_price=price, geometry_json=json.dumps(shapes),
                erp_number=erp_number, code_number=code_number, cutting_service_id=service_id
            )
        else:
            new_detail = Detail(
                name=name, material_key=material_key, width=0.0, height=0.0,
                total_length=0.0, pierce_count=0,
                calculated_price=manual_price, geometry_json=None,
                erp_number=erp_number, code_number=code_number, cutting_service_id=service_id
            )
        db.session.add(new_detail)
        db.session.flush()  # assigns new_detail.id for the DetailDxfFile/Operation FKs below
        if has_dxf:
            _add_cutting_operation(new_detail, service_id, total_length)
            db.session.add(DetailDxfFile(
                detail_id=new_detail.id, filename=stored_filename, original_filename=original_filename,
                uploaded_by_id=current_user.id
            ))
        if pdf_file and pdf_file.filename:
            pdf_stored_filename = _save_upload(pdf_file, app.config['DETAIL_DXF_FOLDER'], allowed_extensions={'pdf'})
            db.session.add(DetailDxfFile(
                detail_id=new_detail.id, filename=pdf_stored_filename,
                original_filename=sanitize_display_filename(pdf_file.filename), uploaded_by_id=current_user.id
            ))
        # Optional extra services (mill, deburr, ...) staged alongside the
        # base cut on the same form - see admin_details.html's operations
        # cart and _add_operations_from_rows().
        extra_ops = _add_operations_from_rows(new_detail, _parse_operations_json(request.form.get('operations_json')))
        db.session.commit()
        log_action(f'Създаден детайл "{name}" (цена {new_detail.calculated_price:g} лв.{", "+str(extra_ops)+" доп. операции" if extra_ops else ""})')
        if extra_ops:
            flash(f'Детайлът "{name}" беше добавен успешно, с {extra_ops} допълнителна операция(и).', 'success')
        else:
            flash(f'Детайлът "{name}" беше добавен успешно.', 'success')
        return redirect(url_for('admin_details'))

    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при обработка/запис: {str(e)}', 'danger')
        if stored_path and os.path.exists(stored_path):
            os.remove(stored_path)

    return redirect(url_for('admin_details'))


@app.route('/details/<int:detail_id>/files')
@login_required
def detail_dxf_dashboard(detail_id):
    """
    A Detail's DXF file repository - reached by clicking the detail's name on
    /admin/details (admins) or by a direct link (any logged-in user, e.g. to
    upload a revision - see the DXF dashboard access decision in the task
    notes). Anyone logged in can view the list and upload, but only admins
    get a working download link - see download_detail_dxf().
    """
    detail = Detail.query.get_or_404(detail_id)
    files = DetailDxfFile.query.filter_by(detail_id=detail_id).order_by(DetailDxfFile.uploaded_at.desc()).all()
    services = Service.query.order_by(Service.name).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    # Plain dicts for the client-side pending-operations cart (see
    # detail_dxf_dashboard.html) to compute a live running total without a
    # round-trip per row - same convention as order_create()'s products_data.
    services_data = [{'id': s.id, 'name': s.name, 'price_per_hour_eur': s.price_per_hour_eur,
                       'pricing_mode': s.pricing_mode, 'price_per_meter_eur': s.price_per_meter_eur}
                      for s in services]
    return render_template('detail_dxf_dashboard.html', detail=detail, files=files, services=services,
                            materials=materials, services_data=services_data, active_page='admin_details')


@app.route('/details/<int:detail_id>/update-material', methods=['POST'])
@login_required
def admin_update_detail_material(detail_id):
    """
    Lets an admin fix a detail's material or cut dimensions (width/height, mm)
    without re-uploading a new DXF - e.g. correcting a wrong material pick.
    total_length/pierce_count stay whatever the original DXF produced, so the
    detail's cutting Operation (priced off total_length, see
    _add_cutting_operation) is untouched; only calculated_price - material
    cost only, see calculate_material_price() - is recomputed.
    """
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    detail = Detail.query.get_or_404(detail_id)
    material_key = request.form.get('material', '')
    material = MaterialPrice.query.filter_by(key=material_key).first()
    if not material:
        flash('Невалиден избор на материал.', 'danger')
        return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))

    try:
        width = float(request.form.get('width', ''))
        height = float(request.form.get('height', ''))
        thickness_raw = request.form.get('thickness', '').strip()
        thickness = float(thickness_raw) if thickness_raw else None
        extra_width_raw = request.form.get('extra_width', '').strip()
        extra_width = float(extra_width_raw) if extra_width_raw else None
        extra_height_raw = request.form.get('extra_height', '').strip()
        extra_height = float(extra_height_raw) if extra_height_raw else None
    except ValueError:
        flash('Моля въведете валидни размери.', 'danger')
        return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))
    if width < 0 or height < 0 or (thickness is not None and thickness < 0) \
            or (extra_width is not None and extra_width < 0) or (extra_height is not None and extra_height < 0):
        flash('Размерите не могат да бъдат отрицателни.', 'danger')
        return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))

    detail.material_key = material_key
    detail.width = width
    detail.height = height
    detail.thickness_mm = thickness
    detail.extra_width_mm = extra_width
    detail.extra_height_mm = extra_height
    detail.calculated_price = calculate_material_price(width + (extra_width or 0), height + (extra_height or 0), material_key)
    log_action(describe_changes(f'детайл "{detail.name}"', detail, {
        'material_key': 'материал', 'width': 'ширина мм', 'height': 'височина мм',
        'thickness_mm': 'дебелина мм', 'extra_width_mm': 'доп. ширина мм',
        'extra_height_mm': 'доп. височина мм', 'calculated_price': 'цена лв.',
    }))
    db.session.commit()
    flash('Материалът и размерите бяха обновени успешно.', 'success')
    return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))


@app.route('/details/<int:detail_id>/files/upload', methods=['POST'])
@login_required
def upload_detail_dxf(detail_id):
    """
    Accepts any file type - not just .dxf. The dashboard is a general file
    repository for a detail (reference photos, spec sheets, revisions...);
    only .dxf files get the 2D preview button (see detail_dxf_dashboard.html
    and get_detail_dxf_geometry() below), everything else just sits there
    for download.

    If the detail has no cut-length/pierce data yet (the "catalogued with a
    manually-entered price, DXF/geometry added later" path admin_add_detail()
    documents), and this upload is a .dxf, parse it and fill that data in -
    otherwise detail_dxf_dashboard.html's "calculate duration from DXF"
    helper (see OP_DEFAULT_LENGTH_MM) always has nothing to work with, since
    nothing else ever populates it after creation. Only fills in *missing*
    geometry, never overwrites existing data - once a detail has real cut
    data, further .dxf uploads here are just reference/revision files like
    any other attachment.
    """
    detail = Detail.query.get_or_404(detail_id)
    file = request.files.get('file')
    if not file or file.filename == '':
        flash(gettext('Моля изберете файл.'), 'danger')
        return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))

    original_filename = sanitize_display_filename(file.filename)
    stored_filename = _save_upload(file, app.config['DETAIL_DXF_FOLDER'])
    db.session.add(DetailDxfFile(
        detail_id=detail.id, filename=stored_filename, original_filename=original_filename,
        uploaded_by_id=current_user.id
    ))

    if not detail.total_length and file.filename.lower().endswith('.dxf'):
        path = os.path.join(app.config['DETAIL_DXF_FOLDER'], stored_filename)
        width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(path)
        if width is not None:
            detail.width = width
            detail.height = height
            detail.total_length = total_length
            detail.pierce_count = pierce_count
            detail.geometry_json = json.dumps(shapes)
            detail.calculated_price = calculate_material_price(width, height, detail.material_key)

    db.session.commit()
    flash(gettext('Файлът беше качен успешно.'), 'success')
    return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))


@app.route('/details/files/<int:file_id>/download')
@role_required('admin')
def download_detail_dxf(file_id):
    """Strictly admin-only, per the access decision - regular users/workers can
    upload to a detail's dashboard but must never be able to pull the raw file back out."""
    dxf_file = DetailDxfFile.query.get_or_404(file_id)
    return send_from_directory(
        app.config['DETAIL_DXF_FOLDER'], dxf_file.filename,
        as_attachment=True, download_name=dxf_file.original_filename
    )


@app.route('/order-item-files/<int:file_id>/download')
@role_required(['admin', 'worker'])
def download_order_item_file(file_id):
    """Admin/staff-only, same access rule as download_detail_dxf() - whoever
    placed the order can attach a reference PDF while building it, but only
    staff (who actually run the operations in production_report.html) can
    pull it back out."""
    attachment = OrderItemAttachment.query.get_or_404(file_id)
    return send_from_directory(
        app.config['ORDER_ITEM_FILE_FOLDER'], attachment.filename,
        as_attachment=True, download_name=attachment.original_filename
    )


@app.route('/details/files/geometry/<int:file_id>')
@role_required('admin')
def get_detail_dxf_geometry(file_id):
    """
    Same admin-only gate as download_detail_dxf() - a rendered preview still
    exposes the design, just not the raw bytes - and the same response shape
    as get_geometry() (see static/js/dxf_viewer.js's openDxfViewer, shared
    between /dashboard and this page via its optional `endpoint` param).
    The id must be the last URL segment - dxf_viewer.js builds the request
    as `${endpoint}${fileId}`, the same convention /geometry/<id> uses.
    Parsed on demand rather than cached, since DXF parsing is cheap and this
    table has no geometry_json column of its own.
    """
    dxf_file = DetailDxfFile.query.get_or_404(file_id)
    if not dxf_file.original_filename.lower().endswith('.dxf'):
        return jsonify({'error': 'Визуализацията е налична само за .dxf файлове.'}), 400

    path = os.path.join(app.config['DETAIL_DXF_FOLDER'], dxf_file.filename)
    try:
        width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(path)
    except Exception:
        width, height, shapes = None, None, []

    if width is None:
        return jsonify({'error': 'Грешка при обработката на DXF структурата.'}), 400

    return jsonify({'filename': dxf_file.original_filename, 'width': width, 'height': height, 'shapes': shapes})


@app.route('/details/files/<int:file_id>/delete', methods=['POST'])
@role_required('admin')
def delete_detail_dxf(file_id):
    dxf_file = DetailDxfFile.query.get_or_404(file_id)
    detail_id = dxf_file.detail_id
    path = os.path.join(app.config['DETAIL_DXF_FOLDER'], dxf_file.filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(dxf_file)
    db.session.commit()
    flash('Файлът беше премахнат успешно.', 'success')
    return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))


def _parse_operations_json(raw):
    """Parses an operations_json string (see admin_add_operation/
    admin_add_detail) into a list of dicts, or [] on any malformed input."""
    try:
        rows = json.loads(raw or '')
        return rows if isinstance(rows, list) else []
    except (TypeError, ValueError):
        return []


def _add_operations_from_rows(detail, rows):
    """Creates one Operation per valid {service_id, duration_minutes} (or
    {service_id, length_mm} for a length-priced Service) row, appended after
    whatever sequence the detail already has - shared by admin_add_operation
    (existing detail) and admin_add_detail (brand-new detail, so
    detail.operations is empty and this just starts at 0). Invalid rows (bad
    service, or a missing/non-positive value for the service's pricing mode)
    are skipped rather than failing the whole batch, same as
    create_delivery_note's items_json. An optional 'description' per row
    (e.g. "външно лазерно рязане" vs. in-house) is stored as-is, trimmed,
    blank -> None. Returns how many were actually added; caller is
    responsible for committing."""
    next_sequence = (max((op.sequence for op in detail.operations), default=-1)) + 1
    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            service_id = int(row.get('service_id'))
        except (TypeError, ValueError):
            continue
        service = db.session.get(Service, service_id)
        if not service:
            continue
        duration_minutes = 0.0
        length_mm = None
        if service.pricing_mode == 'length':
            try:
                length_mm = float(row.get('length_mm'))
            except (TypeError, ValueError):
                continue
            if length_mm <= 0:
                continue
            length_mm = round(length_mm, 2)
        else:
            try:
                duration_minutes = float(row.get('duration_minutes'))
            except (TypeError, ValueError):
                continue
            if duration_minutes <= 0:
                continue
            duration_minutes = round(duration_minutes, 2)
        description = (row.get('description') or '').strip() or None
        db.session.add(Operation(
            detail_id=detail.id, service_id=service_id, sequence=next_sequence,
            duration_minutes=duration_minutes, length_mm=length_mm, description=description
        ))
        next_sequence += 1
        added += 1
    return added


def _add_cutting_operation(detail, service_id, total_length):
    """Attaches the base-cut Operation for a freshly-created Detail (sequence
    0, ahead of any extra ops from _add_operations_from_rows), priced by cut
    length rather than baked into calculated_price - see
    calculate_material_price(). Picking a cutting service is optional and
    isn't required to be length-priced (see admin_add_detail() /
    api_quick_create_detail()) - this only actually attaches an Operation
    when it resolves to a length-priced Service; otherwise cutting_service_id
    is still recorded on the Detail, just without an auto-priced Operation to
    go with it. No-op without a length to cut either (the manual-price/no-DXF
    fallback)."""
    if not service_id or not total_length:
        return
    service = db.session.get(Service, service_id)
    if not service or service.pricing_mode != 'length':
        return
    db.session.add(Operation(
        detail_id=detail.id, service_id=service_id, sequence=0,
        duration_minutes=0.0, length_mm=round(total_length, 2)
    ))


def _order_item_operations_cost(order_item, rows):
    """Creates one OrderItemOperation per valid {service_id, duration_minutes}
    row for a freshly-flushed order_item (always starts sequence at 0, unlike
    _add_operations_from_rows - a new OrderItem never already has operations).
    Invalid rows (bad service/duration) are skipped rather than failing the
    whole cart line. An optional 'description' per row (e.g. "външно лазерно
    рязане" vs. in-house) is stored as-is, trimmed, blank -> None. Returns the
    summed per-unit cost so the caller can fold it into OrderItem.unit_price;
    caller is responsible for committing."""
    total_cost = 0.0
    sequence = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            service_id = int(row.get('service_id'))
            duration_minutes = float(row.get('duration_minutes'))
        except (TypeError, ValueError):
            continue
        service = db.session.get(Service, service_id)
        if duration_minutes <= 0 or not service:
            continue
        description = (row.get('description') or '').strip() or None
        db.session.add(OrderItemOperation(
            order_item_id=order_item.id, service_id=service_id, sequence=sequence,
            duration_minutes=round(duration_minutes, 2), description=description
        ))
        total_cost += duration_minutes * (service.price_per_hour_eur / 60.0)
        sequence += 1
    return round(total_cost, 2)


def _link_order_item_attachment(order_item, attachment):
    """Links a PDF already staged via upload_order_item_pdf() to a freshly-
    flushed order_item. attachment is client-supplied cart data, so its
    filename is re-sanitized and checked against ORDER_ITEM_FILE_FOLDER
    rather than trusted outright - only a name that was actually staged
    there gets linked. No-op on anything malformed or missing."""
    if not isinstance(attachment, dict):
        return
    stored_filename = secure_filename(attachment.get('filename') or '')
    if not stored_filename:
        return
    stored_path = os.path.join(app.config['ORDER_ITEM_FILE_FOLDER'], stored_filename)
    if not os.path.isfile(stored_path):
        return
    original_filename = sanitize_display_filename(attachment.get('original_filename') or stored_filename) or stored_filename
    db.session.add(OrderItemAttachment(
        order_item_id=order_item.id, filename=stored_filename, original_filename=original_filename,
        uploaded_by_id=current_user.id
    ))


@app.route('/admin/details/<int:detail_id>/operations/add', methods=['POST'])
@login_required
def admin_add_operation(detail_id):
    """Attaches one or more extra processing steps (mill, deburr, ...) to a
    Detail beyond its base laser cut, in a single request - see Operation and
    Detail.total_price. operations_json follows [{service_id, duration_minutes},
    ...] - build-a-list-then-submit-once, same convention as
    create_delivery_note's items_json."""
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    detail = Detail.query.get_or_404(detail_id)
    rows = _parse_operations_json(request.form.get('operations_json'))

    if not rows:
        flash('Моля добавете поне една операция.', 'danger')
        return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))

    added = _add_operations_from_rows(detail, rows)

    if not added:
        flash('Няма валидни операции за добавяне.', 'danger')
        return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))

    db.session.commit()
    log_action(f'Добавени {added} операция(и) към детайл "{detail.name}"')
    flash('Добавена е 1 операция.' if added == 1 else f'Добавени са {added} операции.', 'success')
    return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))


@app.route('/admin/operations/<int:operation_id>/delete', methods=['POST'])
@login_required
def admin_delete_operation(operation_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    operation = Operation.query.get_or_404(operation_id)
    detail_id = operation.detail_id
    op_label = f'{operation.service.name} ({operation.detail.name})' if operation.service and operation.detail else str(operation_id)
    db.session.delete(operation)
    db.session.commit()
    log_action(f'Изтрита операция "{op_label}"')
    flash('Операцията беше премахната.', 'success')
    return redirect(url_for('detail_dxf_dashboard', detail_id=detail_id))


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
    old_name = detail.name
    detail.name = name
    db.session.commit()
    log_action(f'Преименуван детайл "{old_name}" → "{name}"')
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

    for dxf_file in detail.dxf_files:
        dxf_path = os.path.join(app.config['DETAIL_DXF_FOLDER'], dxf_file.filename)
        if os.path.exists(dxf_path):
            os.remove(dxf_path)

    name = detail.name
    db.session.delete(detail)
    db.session.commit()
    log_action(f'Изтрит детайл "{name}"')
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
    name_en = request.form.get('name_en', '').strip() or None
    name_de = request.form.get('name_de', '').strip() or None
    description_en = request.form.get('description_en', '').strip() or None
    description_de = request.form.get('description_de', '').strip() or None

    try:
        markup_percent = float(request.form.get('markup_percent', '0') or 0)
    except ValueError:
        markup_percent = 0.0

    new_product = Product(name=name, description=description, markup_percent=round(markup_percent, 2),
                           name_en=name_en, name_de=name_de, description_en=description_en, description_de=description_de)
    db.session.add(new_product)
    db.session.commit()

    log_action(f'Създаден продукт "{name}" (надценка {markup_percent:g}%)')
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
    services = Service.query.order_by(Service.name).all()
    pricing = calculate_product_pricing(product)
    return render_template('product_edit.html', product=product, all_details=all_details, pricing=pricing,
                            materials=materials, services=services, active_page='admin')


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

    # Translation fields only exist on the full admin edit page, not the
    # simpler popup content editor - guard so a popup save (which never
    # submits them) doesn't wipe out translations set earlier.
    if not is_popup:
        product.name_en = request.form.get('name_en', '').strip() or None
        product.name_de = request.form.get('name_de', '').strip() or None
        product.description_en = request.form.get('description_en', '').strip() or None
        product.description_de = request.form.get('description_de', '').strip() or None

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

    log_action(describe_changes(f'продукт "{product.name}"', product, {
        'name': 'име', 'description': 'описание', 'markup_percent': 'надценка %',
        'erp_number': 'ERP №', 'code_number': 'КД №',
    }))
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
    log_action(f'Изтрит продукт "{name}"')
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
    log_action(f'Добавен детайл "{detail.name}" x{quantity} към продукт "{product.name}"')
    flash(f'Детайлът "{detail.name}" беше добавен към продукта.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product.id))


@app.route('/admin/products/<int:product_id>/remove_detail/<int:product_detail_id>', methods=['POST'])
@login_required
def admin_product_remove_detail(product_id, product_detail_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    line_item = ProductDetail.query.filter_by(id=product_detail_id, product_id=product_id).first_or_404()
    detail_name = line_item.detail.name if line_item.detail else str(product_detail_id)
    db.session.delete(line_item)
    db.session.commit()
    log_action(f'Премахнат детайл "{detail_name}" от продукт #{product_id}')
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
    log_action(f'Добавен разход "{label}" ({amount:g} лв.) към продукт "{product.name}"')
    flash(f'Разходът "{label}" беше добавен.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product.id))


@app.route('/admin/products/<int:product_id>/remove_cost/<int:cost_id>', methods=['POST'])
@login_required
def admin_product_remove_cost(product_id, cost_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    cost = ProductExtraCost.query.filter_by(id=cost_id, product_id=product_id).first_or_404()
    label = cost.label
    db.session.delete(cost)
    db.session.commit()
    log_action(f'Премахнат разход "{label}" от продукт #{product_id}')
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
        deleted_username = user_to_delete.username
        db.session.delete(user_to_delete)
        db.session.commit()

        log_action(f'Изтрит потребител "{deleted_username}"')
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


@app.route('/api/upload-order-item-pdf', methods=['POST'])
@login_required
def upload_order_item_pdf():
    """Stages a reference PDF for a not-yet-created OrderItem line, picked from
    the detail+operations section on order_create.html - saved immediately so
    the browser doesn't have to hold the raw File object until the whole cart
    is submitted. create_order() links the staged file to its OrderItem once
    that row is flushed and has an id; a cart line that's staged a PDF and
    then never gets submitted just leaves an orphaned file on disk, same as
    any other abandoned upload in this app."""
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'status': 'error', 'message': gettext('Няма избран файл.')}), 400
    stored_filename = _save_upload(file, app.config['ORDER_ITEM_FILE_FOLDER'], allowed_extensions={'pdf'})
    if not stored_filename:
        return jsonify({'status': 'error', 'message': gettext('Приемат се само PDF файлове.')}), 400
    return jsonify({
        'status': 'success',
        'filename': stored_filename,
        'original_filename': sanitize_display_filename(file.filename),
    })


# МАРШРУТ ЗА ОБИКНОВЕНИ ПОТРЕБИТЕЛИ - СЪЗДАВАНЕ НА ПОРЪЧКА (кошница с няколко артикула)
@app.route('/orders/new', methods=['GET', 'POST'])
@login_required
def create_order():
    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '').strip()
        cart_raw = request.form.get('cart_json', '')

        if not customer_name:
            flash(gettext('Моля въведете име на клиент.'), 'danger')
            return redirect(url_for('create_order'))

        try:
            cart = json.loads(cart_raw)
            if not isinstance(cart, list):
                cart = []
        except (TypeError, ValueError):
            cart = []

        if not cart:
            flash(gettext('Моля добавете поне един артикул към поръчката.'), 'danger')
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
                    quantity_ordered=qty, unit_price=detail.total_price
                )
                db.session.add(order_item)
                db.session.flush()  # need order_item.id for its operations

                op_rows = row.get('operations')
                if isinstance(op_rows, list) and op_rows:
                    ops_cost_per_unit = _order_item_operations_cost(order_item, op_rows)
                    order_item.unit_price = round(order_item.unit_price + ops_cost_per_unit, 2)

                _link_order_item_attachment(order_item, row.get('attachment'))
                added_any = True

        if not added_any:
            db.session.rollback()
            flash(gettext('Невалидни артикули в поръчката.'), 'danger')
            return redirect(url_for('create_order'))

        db.session.commit()
        order_items = OrderItem.query.filter_by(order_id=new_order.id).all()
        header = f'Поръчка {new_order.order_number} за "{customer_name}"'
        if new_order.client:
            header += f' (клиент: {new_order.client.name})'
        item_lines = [f'{oi.item_name}: {oi.quantity_ordered} бр. x {oi.unit_price:g} лв. = {oi.line_total:g} лв.' for oi in order_items]
        log_action(f'Създадена поръчка {new_order.order_number} за "{customer_name}" ({len(order_items)} артикул(и))',
                   details=header + '\n' + '\n'.join(item_lines))
        flash(gettext('Поръчка %(order_number)s беше успешно изпратена!', order_number=new_order.order_number), 'success')

        shortfalls = order_missing_items(new_order)
        if shortfalls:
            missing_desc = '; '.join(
                f"{s['item_name']} (нужни {s['needed']}, налични {s['available']})" for s in shortfalls
            )
            flash(
                gettext('Внимание: поръчка %(order_number)s има недостатъчна наличност за: %(missing_desc)s. Виж таблото "Липсваща наличност".',
                        order_number=new_order.order_number, missing_desc=missing_desc),
                'danger'
            )
        return redirect(url_for('my_orders'))

    products = Product.query.order_by(Product.name).all()
    details = Detail.query.order_by(Detail.name).all()
    machines = Machine.query.order_by(Machine.name).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.type, MaterialPrice.display_name).all()
    services = Service.query.order_by(Service.name).all()
    clients = Client.query.order_by(Client.name).all()
    deliverers = Deliverer.query.order_by(Deliverer.name).all()
    # Pre-computed, JSON-friendly catalogs so the cart UI can add items and
    # show live prices/totals client-side without extra round-trips.
    products_data = [
        {'id': p.id, 'name': localized(p, 'name'), 'price': calculate_product_pricing(p)['sell_price']}
        for p in products
    ]
    details_data = [
        {
            'id': d.id,
            'name': f"{localized(d, 'name')} ({localized(d.material, 'display_name')})" if d.material else localized(d, 'name'),
            'price': d.total_price
        }
        for d in details
    ]
    # JSON-friendly service list so the per-detail operations picker can
    # preview an operation's cost client-side, same convention as
    # admin_details.html's ND_SERVICES.
    services_data = [{'id': s.id, 'name': localized(s, 'name'), 'price_per_hour_eur': s.price_per_hour_eur} for s in services]
    return render_template('order_create.html', products=products_data, details=details_data,
                           machines=machines, materials=materials, services=services, services_data=services_data,
                           clients=clients, deliverers=deliverers,
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
        flash(gettext('Нямате достъп до тази поръчка.'), 'danger')
        return redirect(url_for('my_orders'))

    if not order.can_cancel:
        flash(gettext('Поръчката вече е в процес на изработка (или вече е приключена/отменена) и не може да бъде отменена.'),
              'danger')
        return redirect(url_for('my_orders'))

    order.status = 'cancelled'
    db.session.commit()
    log_action(f'Отменена поръчка {order.order_number}')
    flash(gettext('Поръчка %(order_number)s беше отменена.', order_number=order.order_number), 'success')
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

        # A produced piece is a piece that now physically exists, so moving
        # quantity_produced up (or down, on a correction) moves the linked
        # Detail's stock_quantity by the same delta - same _bump_stock() used
        # by delivery-note intake, just triggered by production instead of a
        # goods-received note. No-op for a product OrderItem's own row (it
        # has no detail) and for a component whose source Detail was since
        # deleted from the catalog (detail_id nullable, only detail_name_snapshot
        # survives).
        if target_type == 'component':
            component = OrderItemComponent.query.get_or_404(target_id)
            produced_qty = max(0, min(produced_qty, component.quantity_needed))
            stock_delta = produced_qty - component.quantity_produced
            component.quantity_produced = produced_qty
            if stock_delta and component.detail:
                _bump_stock(component.detail, stock_delta)
            order_item = component.order_item
            target_percent = component.percent_complete
            target_label = f'детайл "{component.detail.name}"' if component.detail else f'компонент #{target_id}'
        elif target_type == 'item':
            order_item = OrderItem.query.get_or_404(target_id)
            produced_qty = max(0, min(produced_qty, order_item.quantity_ordered))
            stock_delta = produced_qty - order_item.quantity_produced
            order_item.quantity_produced = produced_qty
            if stock_delta and order_item.detail:
                _bump_stock(order_item.detail, stock_delta)
            target_percent = order_item.percent_complete
            target_label = f'артикул "{order_item.item_name}"'
        else:
            return jsonify({'status': 'error', 'message': 'Невалиден тип на артикула.'}), 400

        order = order_item.order
        refresh_order_status(order)
        db.session.commit()
        stock_note = f' - наличността е коригирана с {stock_delta:+d} бр.' if stock_delta else ''
        log_action(f'Изработени {produced_qty} бр. от {target_label} (поръчка {order.order_number}){stock_note}')

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


# АДМИН/РАБОТНИК СТРАНИЦА - ПРОИЗВОДСТВО НА ДЕТАЙЛИ ЗА СКЛАД (guided wizard)
@app.route('/admin/production-orders')
@role_required(['admin', 'worker'])
def admin_production_orders():
    """
    Standalone "произведи N бр. от този детайл" wizard + job list - see
    ProductionOrder. Independent of any customer Order: this is for building
    up Detail.stock_quantity ahead of demand. Precomputes, per Detail, the
    material needed for one unit plus every MaterialPrice row sharing that
    material's display name AND structural type (different price/stock
    batches of the physically same stock from separate delivery notes.
    "Physically the same" means every descriptive field _find_or_create_
    delivery_target() itself matches on (display name, brand, dims, type) -
    name and type alone aren't enough, e.g. two rows can both be called
    "Неръждаема стомана"/sheets while being a different brand and thickness
    (a genuinely different material, not a price lot of the same one) - as
    JSON for the wizard's client-side math - same *_data-as-JSON convention
    as admin_delivery_notes().
    """
    details = Detail.query.order_by(Detail.name).all()
    details_data = [{
        'id': d.id,
        'name': d.name,
        'is_linear': d.material.type in ('rods', 'pipes', 'profiles'),
        'per_piece_qty': _detail_material_unit_qty(d),
        # Price is appended here (not baked into format_material_option()
        # itself, which is shared by every other material <select> in the
        # app) specifically so price-lots that only differ by cost_per_m2
        # (see _find_or_create_delivery_target's price-split) are
        # distinguishable in this one picker.
        'materials': [
            # 'stock' is the AVAILABLE quantity in the same native unit as
            # per_piece_qty (m²/m - see _material_available_qty()), not the
            # raw MaterialPrice.stock_quantity - sheet stock is a sheet
            # count, and showing that raw number next to an area figure
            # would silently compare the wrong units.
            {'id': m.id, 'label': f'{format_material_option(m)} — {m.cost_per_m2:.2f} €{"/м" if m.type in ("rods", "pipes", "profiles") else "/м²"}',
             'stock': round(_material_available_qty(m), 3)}
            for m in MaterialPrice.query.filter_by(
                display_name=d.material.display_name, brand=d.material.brand, type=d.material.type,
                sheet_width_mm=d.material.sheet_width_mm, sheet_length_mm=d.material.sheet_length_mm,
                thickness_mm=d.material.thickness_mm,
            ).order_by(MaterialPrice.cost_per_m2).all()
        ],
    } for d in details]

    pending_jobs = ProductionOrder.query.filter_by(status='pending').order_by(ProductionOrder.created_at.asc()).all()
    # Excludes reversed ('undone') jobs - soft-deleted by delete_production_
    # order() to keep a record for admin_material_history(), but no longer
    # an active/completed job from this page's point of view.
    done_jobs = ProductionOrder.query.filter_by(status='done', reversed_at=None).order_by(ProductionOrder.completed_at.desc()).all()

    return render_template(
        'admin_production_orders.html', details=details, details_data=details_data,
        pending_jobs=pending_jobs, done_jobs=done_jobs, active_page='admin_production_orders'
    )


@app.route('/admin/production-orders/create', methods=['POST'])
@role_required(['admin', 'worker'])
def create_production_order():
    """
    Unlike delivery notes/orders (which never reserve/block stock - see
    order_missing_items()), a planned job here must never be allowed to send
    the chosen material batch's stock below zero - explicit product
    requirement. Rather than rejecting the whole request outright, the
    requested quantity is clamped down to the largest piece count the
    current stock actually covers (floor(stock / per-piece need)), with a
    yellow warning explaining the adjustment; only a genuine zero-stock case
    (can't even make 1) refuses to create the job at all.
    """
    detail = Detail.query.get_or_404(request.form.get('detail_id', type=int))
    material = MaterialPrice.query.get_or_404(request.form.get('material_id', type=int))
    quantity = request.form.get('quantity', type=int)
    if not quantity or quantity < 1:
        flash('Моля въведете валидно количество за производство.', 'danger')
        return redirect(url_for('admin_production_orders'))

    per_piece_qty = _detail_material_unit_qty(detail)
    is_linear = material.type in ('rods', 'pipes', 'profiles')
    unit_label = 'мм' if is_linear else 'м²'
    to_display = (lambda q: round(q * 1000, 1)) if is_linear else (lambda q: round(q, 3))

    # Sheet/other stock is counted in whole raw sheets, not a running m²
    # total - see _material_available_qty(). Comparing planned_qty (an area)
    # against the raw sheet count would be comparing the wrong units.
    available = _material_available_qty(material)
    planned_qty = per_piece_qty * quantity

    if per_piece_qty > 0 and planned_qty > available + 1e-9:
        max_qty = int((available + 1e-9) // per_piece_qty)
        if max_qty < 1:
            flash(f'Няма достатъчна наличност от "{material.display_name}" ({to_display(available)} {unit_label}) '
                  f'за нито един бр. "{detail.name}" (нужни {to_display(per_piece_qty)} {unit_label} на брой) - '
                  f'задачата не беше създадена.', 'warning')
            return redirect(url_for('admin_production_orders'))
        flash(f'Внимание: наличността на "{material.display_name}" ({to_display(available)} {unit_label}) не стига за '
              f'{quantity} бр. "{detail.name}" - количеството беше намалено на {max_qty} бр. '
              f'(максималното възможно, без наличността да падне под нула).', 'warning')
        quantity = max_qty
        planned_qty = per_piece_qty * quantity

    job = ProductionOrder(
        detail_id=detail.id, material_id=material.id, quantity=quantity,
        planned_material_qty=planned_qty, created_by_id=current_user.id,
    )
    db.session.add(job)
    db.session.commit()
    log_action(f'Нова задача за производство: {quantity} бр. "{detail.name}" от "{material.display_name}" '
               f'(нужен материал: {job.planned_display} {job.unit_label})')
    flash(f'Задачата за производство на {quantity} бр. "{detail.name}" беше създадена.', 'success')
    return redirect(url_for('admin_production_orders'))


@app.route('/admin/production-orders/<int:order_id>/complete', methods=['POST'])
@role_required(['admin', 'worker'])
def complete_production_order(order_id):
    """
    Marks a ProductionOrder done: the operator enters how much material was
    actually used (may differ from the planned figure - waste/kerf, see
    ProductionOrder docstring), which is subtracted from the chosen material
    batch's stock, while the planned piece count is added to the Detail's
    stock - both via _bump_stock(), same as delivery-note intake. The
    material figure is entered/stored in its native unit (m² for sheets,
    mm/m for linear stock) but converted to a stock-quantity delta via
    _material_stock_delta() before being applied, since sheet stock is
    counted in whole sheets, not a running m² total.
    """
    job = ProductionOrder.query.get_or_404(order_id)
    if job.status == 'done':
        flash('Тази задача вече е завършена.', 'danger')
        return redirect(url_for('admin_production_orders'))

    actual_display = request.form.get('actual_material_qty', type=float)
    if actual_display is None or actual_display < 0:
        flash('Моля въведете валидно изразходвано количество материал.', 'danger')
        return redirect(url_for('admin_production_orders'))

    actual_qty = actual_display / 1000.0 if job.is_linear_material else actual_display

    job.actual_material_qty = actual_qty
    job.status = 'done'
    job.completed_at = datetime.utcnow()
    job.completed_by_id = current_user.id
    _bump_stock(job.material, -_material_stock_delta(job.material, actual_qty))
    _bump_stock(job.detail, job.quantity)
    db.session.commit()

    log_action(f'Завършена задача за производство: {job.quantity} бр. "{job.detail.name}" - '
               f'изразходвани {job.actual_display} {job.unit_label} от "{job.material.display_name}" '
               f'(нова наличност материал {job.material.stock_quantity:g}, детайл {job.detail.stock_quantity:g})')
    flash(f'Задачата беше отбелязана като завършена - наличностите са обновени.', 'success')
    return redirect(url_for('admin_production_orders'))


@app.route('/admin/production-orders/<int:order_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def delete_production_order(order_id):
    """
    A pending job never touched stock (see create_production_order()), so
    deleting it is a hard delete with no stock effect. A 'done' job DID
    touch stock on completion (complete_production_order()): deleting it
    reverses exactly that - the material batch gets its actual_material_qty
    back (via the same _material_stock_delta() conversion completion used,
    signs flipped), and the Detail loses the `quantity` pieces that were
    credited to it. That job is soft-deleted instead of removed (reversed_at/
    reversed_by_id set, row kept) so admin_material_history() can still show
    both the original "taken for production" and this reversing "returned
    from production" movement - see ProductionOrder's docstring.
    """
    job = ProductionOrder.query.get_or_404(order_id)

    if job.status == 'done':
        if job.reversed_at is not None:
            flash('Тази задача вече е изтрита.', 'danger')
            return redirect(url_for('admin_production_orders'))
        _bump_stock(job.material, _material_stock_delta(job.material, job.actual_material_qty))
        _bump_stock(job.detail, -job.quantity)
        job.reversed_at = datetime.utcnow()
        job.reversed_by_id = current_user.id
        log_action(f'Изтрита завършена задача за производство: {job.quantity} бр. "{job.detail.name}" - '
                   f'наличностите са върнати (материал "{job.material.display_name}" +{job.actual_display} {job.unit_label}, '
                   f'детайл -{job.quantity} бр.)')
        flash('Завършената задача беше изтрита - наличностите бяха върнати към предишните им стойности.', 'success')
    else:
        log_action(f'Изтрита чакаща задача за производство: {job.quantity} бр. "{job.detail.name}"')
        flash('Задачата беше изтрита.', 'success')
        db.session.delete(job)

    db.session.commit()
    return redirect(url_for('admin_production_orders'))


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
    row_label = row.display_name if edit_target_type == 'material' else row.name
    log_action(describe_changes(f'{edit_target_type} "{row_label}"', row, {'erp_number': 'ERP №', 'code_number': 'КД №'}))
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


# ----------------- КОНТРОЛ НА КАЧЕСТВОТО (Quality Control) -----------------

@app.route('/admin/quality')
@role_required(['admin', 'quality_control'])
def admin_quality_control():
    """
    Standalone QC log: pick a Detail/Product, enter one or more measured
    parameters against nominal +/- tolerance, get an auto-scored Годен/
    Негоден result - see QualityCheck/QualityMeasurement. Not tied to a
    specific production batch or order (deliberately - see the models'
    docstrings), just a running inspection history filterable below.
    """
    result_filter = request.args.get('result', '')
    query = QualityCheck.query
    if result_filter in ('pass', 'fail'):
        query = query.filter_by(overall_result=result_filter)
    checks = query.order_by(QualityCheck.created_at.desc()).all()
    details = Detail.query.order_by(Detail.name).all()
    products = Product.query.order_by(Product.name).all()
    instruments = MeasuringInstrument.query.order_by(MeasuringInstrument.name).all()
    return render_template(
        'admin_quality_control.html', checks=checks, details=details, products=products,
        instruments=instruments, result_filter=result_filter, active_page='admin_quality_control',
        edit_check=None, edit_check_data=None
    )


@app.route('/api/quality-template/<target_type>/<int:target_id>')
@role_required(['admin', 'quality_control'])
def api_quality_check_template(target_type, target_id):
    """
    AJAX endpoint: the QualityCheckTemplate explicitly saved for this
    Detail/Product (see admin_save_quality_template()), reused as a
    starting point when it's picked again on admin_quality_control.html -
    same dimension rows (name, nominal, tolerance, tool) and header fields.
    Never sample *values*, since a template doesn't have any.
    """
    if target_type not in ('detail', 'product'):
        return jsonify({'status': 'error', 'message': 'Невалиден тип обект.'}), 400

    template = QualityCheckTemplate.query.filter_by(target_type=target_type, target_id=target_id).first()
    if not template:
        return jsonify({'status': 'success', 'template': None})

    return jsonify({'status': 'success', 'template': {
        'drawing_no': template.drawing_no,
        'batch_size': template.batch_size,
        'sample_size': template.sample_size,
        'iso8015': template.iso8015,
        'measurements': [{
            'parameter_name': m.parameter_name,
            'measurement_type': m.measurement_type,
            'nominal_value': m.nominal_value,
            'tolerance_plus': m.tolerance_plus,
            'tolerance_minus': m.tolerance_minus,
            'unit': m.unit,
            'drawing_ref': m.drawing_ref,
            'instrument_id': m.instrument_id,
        } for m in template.measurements],
    }})


@app.route('/admin/quality/template/save', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_save_quality_template():
    """
    AJAX endpoint behind the QC form's "Запиши като темплейт" button - saves
    (or replaces) the QualityCheckTemplate for whichever Detail/Product is
    currently selected, from the same header fields + measurement-row
    arrays as admin_create_quality_check(), minus the sample values (a
    template has none) and the per-inspection fields (order_no,
    supervisor_name, notes, disposition) which don't belong on a reusable
    template.
    """
    target_type = request.form.get('target_type', '')
    target_id_raw = request.form.get('target_id', '')
    if target_type not in ('detail', 'product') or not target_id_raw.isdigit():
        return jsonify({'status': 'error', 'message': 'Моля изберете детайл или продукт.'}), 400
    target_id = int(target_id_raw)

    target_model = Detail if target_type == 'detail' else Product
    target_row = db.session.get(target_model, target_id)
    if not target_row:
        return jsonify({'status': 'error', 'message': 'Избраният детайл/продукт не съществува.'}), 400

    drawing_no = request.form.get('drawing_no', '').strip() or None
    try:
        batch_size = _parse_optional_float(request.form, 'batch_size')
        batch_size = int(batch_size) if batch_size is not None else None
        sample_size = int(request.form.get('sample_size') or 1)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Размерът на партидата и броят проби трябва да бъдат цели числа.'}), 400
    if sample_size < 1:
        return jsonify({'status': 'error', 'message': 'Броят проби трябва да бъде поне 1.'}), 400

    param_names = request.form.getlist('param_name')
    measurement_types = request.form.getlist('measurement_type')
    nominal_values = request.form.getlist('nominal_value')
    tol_plus_values = request.form.getlist('tolerance_plus')
    tol_minus_values = request.form.getlist('tolerance_minus')
    units = request.form.getlist('unit')
    instrument_ids = request.form.getlist('instrument_id')
    drawing_refs = request.form.getlist('drawing_ref')

    rows = []
    try:
        for i, raw_name in enumerate(param_names):
            name = raw_name.strip()
            if not name:
                continue
            instrument_id_raw = instrument_ids[i] if i < len(instrument_ids) else ''
            rows.append(QualityCheckTemplateMeasurement(
                parameter_name=name,
                measurement_type=(measurement_types[i].strip() if i < len(measurement_types) else '') or None,
                nominal_value=float(nominal_values[i]),
                tolerance_plus=float(tol_plus_values[i] or 0),
                tolerance_minus=float(tol_minus_values[i] or 0),
                unit=units[i].strip() or None,
                drawing_ref=(drawing_refs[i].strip() if i < len(drawing_refs) else '') or None,
                instrument_id=int(instrument_id_raw) if instrument_id_raw.isdigit() else None,
            ))
    except (ValueError, IndexError):
        return jsonify({'status': 'error', 'message': 'Невалидни стойности в измерванията.'}), 400

    if not rows:
        return jsonify({'status': 'error', 'message': 'Добавете поне едно измерване, преди да запишете темплейт.'}), 400

    template = QualityCheckTemplate.query.filter_by(target_type=target_type, target_id=target_id).first()
    if not template:
        template = QualityCheckTemplate(target_type=target_type, target_id=target_id)
        db.session.add(template)

    template.drawing_no = drawing_no
    template.batch_size = batch_size
    template.sample_size = sample_size
    template.iso8015 = request.form.get('iso8015') == 'on'
    template.measurements = rows
    db.session.commit()

    log_action(f'Записан QC темплейт за "{target_row.name}"')
    return jsonify({'status': 'success', 'message': f'Темплейтът за "{target_row.name}" беше запазен.'})


def _parse_quality_check_header(form):
    """
    Shared header-field parsing for admin_create_quality_check()/
    admin_update_quality_check() - target_type/target_id, drawing_no,
    batch_size, sample_size. Returns (data_dict, None) on success or
    (None, error_message) on the first problem found.
    """
    target_type = form.get('target_type', '')
    target_id_raw = form.get('target_id', '')
    if target_type not in ('detail', 'product') or not target_id_raw.isdigit():
        return None, 'Моля изберете детайл или продукт за проверка.'
    target_id = int(target_id_raw)

    target_model = Detail if target_type == 'detail' else Product
    if not db.session.get(target_model, target_id):
        return None, 'Избраният детайл/продукт не съществува.'

    try:
        batch_size = _parse_optional_float(form, 'batch_size')
        batch_size = int(batch_size) if batch_size is not None else None
        sample_size = int(form.get('sample_size') or 1)
    except ValueError:
        return None, 'Размерът на партидата и броят проби трябва да бъдат цели числа.'
    if sample_size < 1:
        return None, 'Броят проби трябва да бъде поне 1.'

    return {
        'target_type': target_type,
        'target_id': target_id,
        'drawing_no': form.get('drawing_no', '').strip() or None,
        'batch_size': batch_size,
        'sample_size': sample_size,
    }, None


def _build_quality_measurements_from_form(form):
    """
    Shared per-row parsing for admin_create_quality_check()/
    admin_update_quality_check() - builds QualityMeasurement (+ QualitySample)
    objects from the QC form's parallel array fields (param_name[],
    measurement_type[], ...). Returns (measurements, None) on success or
    (None, error_message) on the first validation problem found.
    """
    param_names = form.getlist('param_name')
    measurement_types = form.getlist('measurement_type')
    nominal_values = form.getlist('nominal_value')
    tol_plus_values = form.getlist('tolerance_plus')
    tol_minus_values = form.getlist('tolerance_minus')
    units = form.getlist('unit')
    instrument_ids = form.getlist('instrument_id')
    drawing_refs = form.getlist('drawing_ref')

    measurements = []
    try:
        for i, raw_name in enumerate(param_names):
            name = raw_name.strip()
            if not name:
                continue
            instrument_id_raw = instrument_ids[i] if i < len(instrument_ids) else ''
            drawing_ref = drawing_refs[i].strip() if i < len(drawing_refs) else ''
            measurement_type = measurement_types[i].strip() if i < len(measurement_types) else ''

            samples = []
            for sample_index, raw_value in enumerate(form.getlist(f'sample_row{i}'), start=1):
                raw_value = raw_value.strip()
                if raw_value == '':
                    continue
                samples.append(QualitySample(sample_index=sample_index, value=float(raw_value)))
            if not samples:
                return None, f'Измерване "{name}" няма нито една въведена проба.'

            measurements.append(QualityMeasurement(
                parameter_name=name,
                measurement_type=measurement_type or None,
                nominal_value=float(nominal_values[i]),
                tolerance_plus=float(tol_plus_values[i] or 0),
                tolerance_minus=float(tol_minus_values[i] or 0),
                unit=units[i].strip() or None,
                instrument_id=int(instrument_id_raw) if instrument_id_raw.isdigit() else None,
                drawing_ref=drawing_ref or None,
                samples=samples,
            ))
    except (ValueError, IndexError):
        return None, 'Невалидни стойности в измерванията - проверете дали всички числови полета са попълнени коректно.'

    if not measurements:
        return None, 'Добавете поне едно измерване.'

    return measurements, None


@app.route('/admin/quality/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_create_quality_check():
    header, error = _parse_quality_check_header(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_quality_control'))

    measurements, error = _build_quality_measurements_from_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_quality_control'))

    overall_result = 'pass' if all(m.is_within_tolerance for m in measurements) else 'fail'

    check = QualityCheck(
        target_type=header['target_type'], target_id=header['target_id'], inspector_id=current_user.id,
        notes=request.form.get('notes', '').strip() or None, overall_result=overall_result, measurements=measurements,
        iso8015=request.form.get('iso8015') == 'on',
        drawing_no=header['drawing_no'], batch_size=header['batch_size'], sample_size=header['sample_size'],
        order_no=request.form.get('order_no', '').strip() or None,
        disposition=','.join(request.form.getlist('disposition')) or None,
        supervisor_name=request.form.get('supervisor_name', '').strip() or None,
    )
    db.session.add(check)
    db.session.commit()

    result_label = 'Годен' if overall_result == 'pass' else 'Негоден'
    log_action(f'Нова QC проверка на "{check.target_name}" - резултат {result_label}')
    flash(f'QC проверката беше записана ({result_label}).', 'success' if overall_result == 'pass' else 'danger')
    return redirect(url_for('admin_quality_control'))


@app.route('/admin/quality/<int:check_id>/edit')
@role_required(['admin', 'quality_control'])
def admin_edit_quality_check(check_id):
    """
    Reuses admin_quality_control.html's "new check" form, pre-filled with an
    existing QualityCheck's data (including sample values, unlike a
    QualityCheckTemplate which never has any) - so a data-entry mistake can
    be fixed without deleting and retyping the whole inspection.
    """
    check = QualityCheck.query.get_or_404(check_id)
    result_filter = request.args.get('result', '')
    query = QualityCheck.query
    if result_filter in ('pass', 'fail'):
        query = query.filter_by(overall_result=result_filter)
    checks = query.order_by(QualityCheck.created_at.desc()).all()
    details = Detail.query.order_by(Detail.name).all()
    products = Product.query.order_by(Product.name).all()
    instruments = MeasuringInstrument.query.order_by(MeasuringInstrument.name).all()

    edit_check_data = {
        'id': check.id,
        'target_type': check.target_type,
        'target_id': check.target_id,
        'drawing_no': check.drawing_no,
        'batch_size': check.batch_size,
        'sample_size': check.sample_size,
        'order_no': check.order_no,
        'supervisor_name': check.supervisor_name,
        'notes': check.notes,
        'iso8015': check.iso8015,
        'disposition': (check.disposition or '').split(',') if check.disposition else [],
        'measurements': [{
            'parameter_name': m.parameter_name,
            'measurement_type': m.measurement_type,
            'nominal_value': m.nominal_value,
            'tolerance_plus': m.tolerance_plus,
            'tolerance_minus': m.tolerance_minus,
            'unit': m.unit,
            'drawing_ref': m.drawing_ref,
            'instrument_id': m.instrument_id,
            'samples': [
                (m.sample_map.get(idx).value if m.sample_map.get(idx) else None)
                for idx in range(1, check.sample_size + 1)
            ],
        } for m in check.measurements],
    }

    return render_template(
        'admin_quality_control.html', checks=checks, details=details, products=products,
        instruments=instruments, result_filter=result_filter, active_page='admin_quality_control',
        edit_check=check, edit_check_data=edit_check_data
    )


@app.route('/admin/quality/<int:check_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_quality_check(check_id):
    check = QualityCheck.query.get_or_404(check_id)

    header, error = _parse_quality_check_header(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_edit_quality_check', check_id=check_id))

    measurements, error = _build_quality_measurements_from_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_edit_quality_check', check_id=check_id))

    overall_result = 'pass' if all(m.is_within_tolerance for m in measurements) else 'fail'

    check.target_type = header['target_type']
    check.target_id = header['target_id']
    check.notes = request.form.get('notes', '').strip() or None
    check.overall_result = overall_result
    check.measurements = measurements
    check.iso8015 = request.form.get('iso8015') == 'on'
    check.drawing_no = header['drawing_no']
    check.batch_size = header['batch_size']
    check.sample_size = header['sample_size']
    check.order_no = request.form.get('order_no', '').strip() or None
    check.disposition = ','.join(request.form.getlist('disposition')) or None
    check.supervisor_name = request.form.get('supervisor_name', '').strip() or None
    db.session.commit()

    result_label = 'Годен' if overall_result == 'pass' else 'Негоден'
    log_action(f'Редактирана QC проверка #{check.id} на "{check.target_name}" - резултат {result_label}')
    flash(f'QC проверката беше обновена ({result_label}).', 'success' if overall_result == 'pass' else 'danger')
    return redirect(url_for('admin_quality_control'))


@app.route('/admin/quality/<int:check_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_quality_check(check_id):
    check = QualityCheck.query.get_or_404(check_id)
    target_name = check.target_name
    db.session.delete(check)
    db.session.commit()
    log_action(f'Изтрита QC проверка на "{target_name}"')
    flash('QC проверката беше изтрита.', 'success')
    return redirect(url_for('admin_quality_control'))


@app.route('/admin/quality/<int:check_id>/print')
@role_required(['admin', 'quality_control'])
def admin_quality_check_print(check_id):
    """Browser-print view (Ctrl+P to PDF, no server-side PDF library) styled
    after the shop's paper "Mechanical Inspection Report" form - same
    pattern as offer.html/protocol.html/certificate.html."""
    check = QualityCheck.query.get_or_404(check_id)
    sample_indexes = list(range(1, check.sample_size + 1))
    return render_template('admin_quality_check_print.html', check=check, sample_indexes=sample_indexes)


@app.route('/admin/quality/template/<target_type>/<int:target_id>/print')
@role_required(['admin', 'quality_control'])
def admin_quality_template_print(target_type, target_id):
    """
    Blank printable inspection sheet from a QualityCheckTemplate - same
    layout as admin_quality_check_print.html (Blue Print Dimension/
    Tolerance/Nominal columns filled from the template) but with empty
    sample/result/verdict cells, meant to be handed to the shop floor and
    filled in by hand before the readings are typed into a real QC check.
    """
    if target_type not in ('detail', 'product'):
        flash('Невалиден тип обект.', 'danger')
        return redirect(url_for('admin_quality_control'))

    template = QualityCheckTemplate.query.filter_by(target_type=target_type, target_id=target_id).first()
    if not template:
        flash('Няма записан темплейт за избрания детайл/продукт - запишете го първо с "Запиши като темплейт".', 'danger')
        return redirect(url_for('admin_quality_control'))

    target_model = Detail if target_type == 'detail' else Product
    target_row = db.session.get(target_model, target_id)
    target_name = target_row.name if target_row else f'#{target_id}'
    sample_indexes = list(range(1, template.sample_size + 1))
    return render_template(
        'admin_quality_template_print.html', template=template, target_name=target_name, sample_indexes=sample_indexes
    )


@app.route('/admin/quality/instruments')
@role_required(['admin', 'quality_control'])
def admin_measuring_instruments():
    """
    Catalog page for MeasuringInstrument - the tools picked per row on the
    QC check form (see admin_quality_control.html). Separate page rather
    than folded into that form, since the QC page only needs to add one
    on the fly (see api_quick_create_instrument) while this is the full
    add/edit/delete view of the catalog.
    """
    instruments = MeasuringInstrument.query.order_by(MeasuringInstrument.name).all()
    return render_template(
        'admin_measuring_instruments.html', instruments=instruments, active_page='admin_measuring_instruments'
    )


@app.route('/admin/quality/instruments/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_measuring_instrument():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на инструмента.', 'danger')
        return redirect(url_for('admin_measuring_instruments'))

    accuracy_value = _parse_optional_float(request.form, 'accuracy_value')
    interval_raw = request.form.get('calibration_interval_months', '')
    instrument = MeasuringInstrument(
        name=name,
        description=request.form.get('description', '').strip() or None,
        accuracy_value=accuracy_value,
        accuracy_unit=request.form.get('accuracy_unit', '').strip() or None,
        calibration_interval_months=int(interval_raw) if interval_raw.isdigit() else None,
    )
    db.session.add(instrument)
    db.session.commit()
    log_action(f'Създаден измервателен инструмент "{name}"')
    flash(f'Инструментът "{name}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_measuring_instruments'))


@app.route('/admin/quality/instruments/<int:instrument_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_measuring_instrument(instrument_id):
    instrument = MeasuringInstrument.query.get_or_404(instrument_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на инструмента.', 'danger')
        return redirect(url_for('admin_measuring_instruments'))

    interval_raw = request.form.get('calibration_interval_months', '')
    instrument.name = name
    instrument.description = request.form.get('description', '').strip() or None
    instrument.accuracy_value = _parse_optional_float(request.form, 'accuracy_value')
    instrument.accuracy_unit = request.form.get('accuracy_unit', '').strip() or None
    instrument.calibration_interval_months = int(interval_raw) if interval_raw.isdigit() else None
    db.session.commit()
    log_action(f'Обновен измервателен инструмент "{name}"')
    flash(f'Инструментът "{name}" беше обновен успешно.', 'success')
    return redirect(url_for('admin_measuring_instruments'))


@app.route('/admin/quality/instruments/<int:instrument_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_measuring_instrument(instrument_id):
    instrument = MeasuringInstrument.query.get_or_404(instrument_id)
    if QualityMeasurement.query.filter_by(instrument_id=instrument.id).first():
        flash(f'Инструментът "{instrument.name}" не може да бъде изтрит - използван е в записани QC измервания.', 'danger')
        return redirect(url_for('admin_measuring_instruments'))

    name = instrument.name
    db.session.delete(instrument)
    db.session.commit()
    log_action(f'Изтрит измервателен инструмент "{name}"')
    flash(f'Инструментът "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_measuring_instruments'))


@app.route('/admin/quality/instruments/<int:instrument_id>/calibrate', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_calibration_record(instrument_id):
    """
    Logs a calibration/verification event for an instrument (ISO 9001
    §7.1.5) - kept forever in InstrumentCalibrationRecord, never edited/
    overwritten, so the calibration history stays auditable.
    """
    instrument = MeasuringInstrument.query.get_or_404(instrument_id)

    calibrated_at_raw = request.form.get('calibrated_at', '').strip()
    if not calibrated_at_raw:
        flash('Моля въведете дата на калибриране.', 'danger')
        return redirect(url_for('admin_measuring_instruments'))
    try:
        calibrated_at = datetime.strptime(calibrated_at_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата на калибриране.', 'danger')
        return redirect(url_for('admin_measuring_instruments'))

    next_due_date = None
    next_due_raw = request.form.get('next_due_date', '').strip()
    if next_due_raw:
        try:
            next_due_date = datetime.strptime(next_due_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна дата за следващо калибриране.', 'danger')
            return redirect(url_for('admin_measuring_instruments'))

    file = request.files.get('file')
    stored_filename = _save_upload(file, app.config['CALIBRATION_CERT_FOLDER'])
    original_filename = sanitize_display_filename(file.filename) if stored_filename else None

    db.session.add(InstrumentCalibrationRecord(
        instrument_id=instrument.id, calibrated_at=calibrated_at, next_due_date=next_due_date,
        calibrated_by=request.form.get('calibrated_by', '').strip() or None,
        certificate_no=request.form.get('certificate_no', '').strip() or None,
        notes=request.form.get('notes', '').strip() or None,
        filename=stored_filename, original_filename=original_filename, recorded_by_id=current_user.id,
    ))
    db.session.commit()

    log_action(f'Записано калибриране на "{instrument.name}" ({calibrated_at.strftime("%d.%m.%Y")})')
    flash(f'Калибрирането на "{instrument.name}" беше записано.', 'success')
    return redirect(url_for('admin_measuring_instruments'))


@app.route('/admin/quality/instruments/calibration/<int:record_id>/download')
@role_required(['admin', 'quality_control'])
def admin_download_calibration_certificate(record_id):
    record = InstrumentCalibrationRecord.query.get_or_404(record_id)
    if not record.filename:
        flash('Този запис няма прикачен сертификат.', 'danger')
        return redirect(url_for('admin_measuring_instruments'))
    return send_from_directory(
        app.config['CALIBRATION_CERT_FOLDER'], record.filename,
        as_attachment=True, download_name=record.original_filename
    )


# ----------------- ISO 9001 - ДОКУМЕНТАЛЕН КОНТРОЛ (§7.5) -----------------

@app.route('/admin/documents')
@role_required(['admin', 'worker', 'quality_control'])
def admin_documents():
    """
    Controlled-document catalog (ISO 9001 §7.5) - quality manual/policies/
    procedures/work instructions/forms/records, each with a full revision
    history (see ControlledDocumentRevision). Viewable by any staff role;
    only admin/quality_control may add/edit/upload revisions - see the
    mutating routes below.
    """
    documents = ControlledDocument.query.order_by(ControlledDocument.document_no).all()
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_documents.html', documents=documents, users=users,
        categories=CONTROLLED_DOCUMENT_CATEGORIES, statuses=CONTROLLED_DOCUMENT_STATUSES,
        active_page='admin_documents'
    )


@app.route('/admin/documents/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_document():
    document_no = request.form.get('document_no', '').strip()
    title = request.form.get('title', '').strip()
    if not document_no or not title:
        flash('Моля въведете номер и заглавие на документа.', 'danger')
        return redirect(url_for('admin_documents'))

    if ControlledDocument.query.filter_by(document_no=document_no).first():
        flash(f'Вече съществува документ с номер "{document_no}".', 'danger')
        return redirect(url_for('admin_documents'))

    category = request.form.get('category', 'procedure')
    if category not in CONTROLLED_DOCUMENT_CATEGORIES:
        category = 'procedure'
    status = request.form.get('status', 'active')
    if status not in CONTROLLED_DOCUMENT_STATUSES:
        status = 'active'

    owner_id_raw = request.form.get('owner_id', '')
    owner_id = int(owner_id_raw) if owner_id_raw.isdigit() else None

    effective_date = None
    effective_date_raw = request.form.get('effective_date', '').strip()
    if effective_date_raw:
        try:
            effective_date = datetime.strptime(effective_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна дата на влизане в сила.', 'danger')
            return redirect(url_for('admin_documents'))

    revision_label = request.form.get('revision_label', '').strip() or '1'
    file = request.files.get('file')
    stored_filename = _save_upload(file, app.config['CONTROLLED_DOCUMENT_FOLDER'])
    original_filename = sanitize_display_filename(file.filename) if stored_filename else None

    document = ControlledDocument(
        document_no=document_no, title=title, category=category, status=status,
        current_revision=revision_label, owner_id=owner_id,
        approved_by=request.form.get('approved_by', '').strip() or None,
        effective_date=effective_date, filename=stored_filename, original_filename=original_filename,
    )
    db.session.add(document)
    db.session.flush()
    db.session.add(ControlledDocumentRevision(
        document_id=document.id, revision_label=revision_label,
        change_description='Първоначално издаване', filename=stored_filename, original_filename=original_filename,
        approved_by=document.approved_by, revised_by_id=current_user.id,
    ))
    db.session.commit()

    log_action(f'Създаден документ "{document_no} - {title}" (рев. {revision_label})')
    flash(f'Документ "{document_no}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:document_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_document(document_id):
    document = ControlledDocument.query.get_or_404(document_id)

    title = request.form.get('title', '').strip()
    if not title:
        flash('Моля въведете заглавие на документа.', 'danger')
        return redirect(url_for('admin_documents'))

    category = request.form.get('category', document.category)
    if category not in CONTROLLED_DOCUMENT_CATEGORIES:
        category = document.category
    status = request.form.get('status', document.status)
    if status not in CONTROLLED_DOCUMENT_STATUSES:
        status = document.status

    owner_id_raw = request.form.get('owner_id', '')
    document.title = title
    document.category = category
    document.status = status
    document.owner_id = int(owner_id_raw) if owner_id_raw.isdigit() else None
    document.approved_by = request.form.get('approved_by', '').strip() or None

    effective_date_raw = request.form.get('effective_date', '').strip()
    if effective_date_raw:
        try:
            document.effective_date = datetime.strptime(effective_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна дата на влизане в сила.', 'danger')
            return redirect(url_for('admin_documents'))
    else:
        document.effective_date = None

    db.session.commit()
    log_action(f'Обновени данни на документ "{document.document_no}"')
    flash(f'Документ "{document.document_no}" беше обновен.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:document_id>/new-revision', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_document_revision(document_id):
    """
    Uploads a new version of a controlled document - the document's own
    filename/current_revision are updated to this version, but the previous
    version stays on disk and in ControlledDocumentRevision for the audit
    trail (§7.5.3 control of changes), never overwritten.
    """
    document = ControlledDocument.query.get_or_404(document_id)

    revision_label = request.form.get('revision_label', '').strip()
    if not revision_label:
        flash('Моля въведете номер/означение на новата версия.', 'danger')
        return redirect(url_for('admin_documents'))

    file = request.files.get('file')
    stored_filename = _save_upload(file, app.config['CONTROLLED_DOCUMENT_FOLDER'])
    original_filename = sanitize_display_filename(file.filename) if stored_filename else None
    approved_by = request.form.get('approved_by', '').strip() or None

    db.session.add(ControlledDocumentRevision(
        document_id=document.id, revision_label=revision_label,
        change_description=request.form.get('change_description', '').strip() or None,
        filename=stored_filename, original_filename=original_filename,
        approved_by=approved_by, revised_by_id=current_user.id,
    ))
    document.current_revision = revision_label
    document.approved_by = approved_by or document.approved_by
    if stored_filename:
        document.filename = stored_filename
        document.original_filename = original_filename
    db.session.commit()

    log_action(f'Нова версия на документ "{document.document_no}" - рев. {revision_label}')
    flash(f'Версия {revision_label} на "{document.document_no}" беше записана.', 'success')
    return redirect(url_for('admin_documents'))


@app.route('/admin/documents/<int:document_id>/download')
@role_required(['admin', 'worker', 'quality_control'])
def admin_download_document(document_id):
    document = ControlledDocument.query.get_or_404(document_id)
    if not document.filename:
        flash('Този документ няма прикачен файл.', 'danger')
        return redirect(url_for('admin_documents'))
    return send_from_directory(
        app.config['CONTROLLED_DOCUMENT_FOLDER'], document.filename,
        as_attachment=True, download_name=document.original_filename
    )


@app.route('/admin/documents/revisions/<int:revision_id>/download')
@role_required(['admin', 'worker', 'quality_control'])
def admin_download_document_revision(revision_id):
    revision = ControlledDocumentRevision.query.get_or_404(revision_id)
    if not revision.filename:
        flash('Тази версия няма прикачен файл.', 'danger')
        return redirect(url_for('admin_documents'))
    return send_from_directory(
        app.config['CONTROLLED_DOCUMENT_FOLDER'], revision.filename,
        as_attachment=True, download_name=revision.original_filename
    )


@app.route('/admin/documents/<int:document_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_document(document_id):
    document = ControlledDocument.query.get_or_404(document_id)
    for revision in document.revisions:
        if revision.filename:
            path = os.path.join(app.config['CONTROLLED_DOCUMENT_FOLDER'], revision.filename)
            if os.path.exists(path):
                os.remove(path)

    document_no = document.document_no
    db.session.delete(document)
    db.session.commit()
    log_action(f'Изтрит документ "{document_no}"')
    flash(f'Документ "{document_no}" беше изтрит.', 'success')
    return redirect(url_for('admin_documents'))


# ----------------- ISO 9001 - CAPA (§10.2) -----------------

def _apply_capa_form(capa, form):
    """Shared field-assignment for admin_add_capa()/admin_update_capa() -
    every CapaRecord field is optional except problem_description (a CAPA
    can be opened with just the problem, filled in over its lifecycle)."""
    responsible_id_raw = form.get('responsible_id', '')
    status = form.get('status', 'open')
    if status not in CAPA_STATUSES:
        status = 'open'

    capa.source_description = form.get('source_description', '').strip() or None
    capa.problem_description = form.get('problem_description', '').strip()
    capa.containment_action = form.get('containment_action', '').strip() or None
    capa.root_cause = form.get('root_cause', '').strip() or None
    capa.corrective_action = form.get('corrective_action', '').strip() or None
    capa.preventive_action = form.get('preventive_action', '').strip() or None
    capa.responsible_id = int(responsible_id_raw) if responsible_id_raw.isdigit() else None
    capa.status = status
    capa.verification_notes = form.get('verification_notes', '').strip() or None
    capa.verified_by = form.get('verified_by', '').strip() or None

    for field, attr in (('due_date', 'due_date'), ('verified_at', 'verified_at')):
        raw = form.get(field, '').strip()
        if raw:
            setattr(capa, attr, datetime.strptime(raw, '%Y-%m-%d').date())
        else:
            setattr(capa, attr, None)


@app.route('/admin/capa')
@role_required(['admin', 'worker', 'quality_control'])
def admin_capa():
    """
    ISO 9001 §10.2 corrective/preventive action log - viewable by any staff
    role (workers often ARE the responsible party executing an action);
    only admin/quality_control may create/edit/delete - see the mutating
    routes below.
    """
    status_filter = request.args.get('status', '')
    query = CapaRecord.query
    if status_filter in CAPA_STATUSES:
        query = query.filter_by(status=status_filter)
    records = query.order_by(CapaRecord.created_at.desc()).all()
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_capa.html', records=records, users=users, statuses=CAPA_STATUSES,
        status_filter=status_filter, active_page='admin_capa',
        prefill_problem=request.args.get('prefill_problem', ''),
        prefill_source=request.args.get('prefill_source', ''),
        from_finding_id=request.args.get('from_finding_id', ''),
        from_review_action_id=request.args.get('from_review_action_id', ''),
        from_risk_entry_id=request.args.get('from_risk_entry_id', ''),
        from_satisfaction_id=request.args.get('from_satisfaction_id', ''),
    )


@app.route('/admin/capa/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_capa():
    if not request.form.get('problem_description', '').strip():
        flash('Моля опишете несъответствието/проблема.', 'danger')
        return redirect(url_for('admin_capa'))

    try:
        capa = CapaRecord(capa_no=_next_capa_number(), opened_by_id=current_user.id)
        _apply_capa_form(capa, request.form)
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_capa'))

    db.session.add(capa)
    db.session.flush()

    from_finding_id = request.form.get('from_finding_id', '')
    if from_finding_id.isdigit():
        finding = db.session.get(AuditFinding, int(from_finding_id))
        if finding and not finding.capa_id:
            finding.capa_id = capa.id

    from_review_action_id = request.form.get('from_review_action_id', '')
    if from_review_action_id.isdigit():
        review_action = db.session.get(ManagementReviewAction, int(from_review_action_id))
        if review_action and not review_action.capa_id:
            review_action.capa_id = capa.id

    from_risk_entry_id = request.form.get('from_risk_entry_id', '')
    if from_risk_entry_id.isdigit():
        risk_entry = db.session.get(RiskRegisterEntry, int(from_risk_entry_id))
        if risk_entry and not risk_entry.capa_id:
            risk_entry.capa_id = capa.id

    from_satisfaction_id = request.form.get('from_satisfaction_id', '')
    if from_satisfaction_id.isdigit():
        satisfaction_record = db.session.get(CustomerSatisfactionRecord, int(from_satisfaction_id))
        if satisfaction_record and not satisfaction_record.capa_id:
            satisfaction_record.capa_id = capa.id

    db.session.commit()
    log_action(f'Отворен CAPA запис "{capa.capa_no}"')
    flash(f'CAPA запис "{capa.capa_no}" беше създаден.', 'success')
    return redirect(url_for('admin_capa'))


@app.route('/admin/capa/<int:capa_id>/edit')
@role_required(['admin', 'worker', 'quality_control'])
def admin_edit_capa(capa_id):
    capa = CapaRecord.query.get_or_404(capa_id)
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_capa_edit.html', capa=capa, users=users, statuses=CAPA_STATUSES, active_page='admin_capa'
    )


@app.route('/admin/capa/<int:capa_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_capa(capa_id):
    capa = CapaRecord.query.get_or_404(capa_id)
    if not request.form.get('problem_description', '').strip():
        flash('Моля опишете несъответствието/проблема.', 'danger')
        return redirect(url_for('admin_edit_capa', capa_id=capa_id))

    try:
        _apply_capa_form(capa, request.form)
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_edit_capa', capa_id=capa_id))

    db.session.commit()
    log_action(f'Обновен CAPA запис "{capa.capa_no}" - статус {capa.status_label}')
    flash(f'CAPA запис "{capa.capa_no}" беше обновен.', 'success')
    return redirect(url_for('admin_capa'))


@app.route('/admin/capa/<int:capa_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_capa(capa_id):
    capa = CapaRecord.query.get_or_404(capa_id)
    capa_no = capa.capa_no
    # Unlink first (not cascade-delete) - an audit finding/review action/risk
    # entry is its own record independent of whether the CAPA opened from it
    # still exists; deleting the CAPA should only clear the reference, same
    # as it never having one.
    for finding in AuditFinding.query.filter_by(capa_id=capa.id).all():
        finding.capa_id = None
    for review_action in ManagementReviewAction.query.filter_by(capa_id=capa.id).all():
        review_action.capa_id = None
    for risk_entry in RiskRegisterEntry.query.filter_by(capa_id=capa.id).all():
        risk_entry.capa_id = None
    for satisfaction_record in CustomerSatisfactionRecord.query.filter_by(capa_id=capa.id).all():
        satisfaction_record.capa_id = None
    db.session.delete(capa)
    db.session.commit()
    log_action(f'Изтрит CAPA запис "{capa_no}"')
    flash(f'CAPA запис "{capa_no}" беше изтрит.', 'success')
    return redirect(url_for('admin_capa'))


# ----------------- ISO 9001 - ВЪТРЕШНИ ОДИТИ (§9.2) -----------------

@app.route('/admin/audits')
@role_required(['admin', 'worker', 'quality_control'])
def admin_audits():
    """ISO 9001 §9.2 internal audit log - viewable by any staff role; only
    admin/quality_control may create/edit/delete - see the mutating routes
    on this and admin_edit_audit()."""
    status_filter = request.args.get('status', '')
    query = InternalAudit.query
    if status_filter in AUDIT_STATUSES:
        query = query.filter_by(status=status_filter)
    audits = query.order_by(InternalAudit.created_at.desc()).all()
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_audits.html', audits=audits, users=users, statuses=AUDIT_STATUSES,
        status_filter=status_filter, active_page='admin_audits'
    )


@app.route('/admin/audits/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_audit():
    scope = request.form.get('scope', '').strip()
    if not scope:
        flash('Моля въведете обхват на одита.', 'danger')
        return redirect(url_for('admin_audits'))

    auditor_id_raw = request.form.get('auditor_id', '')
    planned_date = None
    planned_date_raw = request.form.get('planned_date', '').strip()
    if planned_date_raw:
        try:
            planned_date = datetime.strptime(planned_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна планирана дата.', 'danger')
            return redirect(url_for('admin_audits'))

    audit = InternalAudit(
        audit_no=_next_audit_number(), scope=scope,
        auditor_id=int(auditor_id_raw) if auditor_id_raw.isdigit() else None,
        planned_date=planned_date, created_by_id=current_user.id,
    )
    db.session.add(audit)
    db.session.commit()
    log_action(f'Създаден одит "{audit.audit_no}"')
    flash(f'Одит "{audit.audit_no}" беше създаден.', 'success')
    return redirect(url_for('admin_audits'))


@app.route('/admin/audits/<int:audit_id>/edit')
@role_required(['admin', 'worker', 'quality_control'])
def admin_edit_audit(audit_id):
    audit = InternalAudit.query.get_or_404(audit_id)
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_audit_edit.html', audit=audit, users=users, statuses=AUDIT_STATUSES,
        finding_types=AUDIT_FINDING_TYPES, active_page='admin_audits'
    )


@app.route('/admin/audits/<int:audit_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_audit(audit_id):
    audit = InternalAudit.query.get_or_404(audit_id)
    scope = request.form.get('scope', '').strip()
    if not scope:
        flash('Моля въведете обхват на одита.', 'danger')
        return redirect(url_for('admin_edit_audit', audit_id=audit_id))

    status = request.form.get('status', audit.status)
    if status not in AUDIT_STATUSES:
        status = audit.status
    auditor_id_raw = request.form.get('auditor_id', '')

    try:
        for field in ('planned_date', 'actual_date'):
            raw = request.form.get(field, '').strip()
            setattr(audit, field, datetime.strptime(raw, '%Y-%m-%d').date() if raw else None)
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_edit_audit', audit_id=audit_id))

    audit.scope = scope
    audit.status = status
    audit.auditor_id = int(auditor_id_raw) if auditor_id_raw.isdigit() else None
    audit.summary = request.form.get('summary', '').strip() or None
    db.session.commit()
    log_action(f'Обновен одит "{audit.audit_no}" - статус {audit.status_label}')
    flash(f'Одит "{audit.audit_no}" беше обновен.', 'success')
    return redirect(url_for('admin_edit_audit', audit_id=audit_id))


@app.route('/admin/audits/<int:audit_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_audit(audit_id):
    audit = InternalAudit.query.get_or_404(audit_id)
    audit_no = audit.audit_no
    db.session.delete(audit)
    db.session.commit()
    log_action(f'Изтрит одит "{audit_no}"')
    flash(f'Одит "{audit_no}" беше изтрит.', 'success')
    return redirect(url_for('admin_audits'))


@app.route('/admin/audits/<int:audit_id>/findings/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_audit_finding(audit_id):
    audit = InternalAudit.query.get_or_404(audit_id)
    description = request.form.get('description', '').strip()
    if not description:
        flash('Моля въведете описание на констатацията.', 'danger')
        return redirect(url_for('admin_edit_audit', audit_id=audit_id))

    finding_type = request.form.get('finding_type', 'observation')
    if finding_type not in AUDIT_FINDING_TYPES:
        finding_type = 'observation'

    db.session.add(AuditFinding(
        audit_id=audit.id, finding_type=finding_type, description=description,
        clause_reference=request.form.get('clause_reference', '').strip() or None,
    ))
    db.session.commit()
    log_action(f'Нова констатация към одит "{audit.audit_no}"')
    flash('Констатацията беше добавена.', 'success')
    return redirect(url_for('admin_edit_audit', audit_id=audit_id))


@app.route('/admin/audits/findings/<int:finding_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_audit_finding(finding_id):
    finding = AuditFinding.query.get_or_404(finding_id)
    audit_id = finding.audit_id
    db.session.delete(finding)
    db.session.commit()
    flash('Констатацията беше изтрита.', 'success')
    return redirect(url_for('admin_edit_audit', audit_id=audit_id))


# ----------------- ISO 9001 - ПРЕГЛЕД ОТ РЪКОВОДСТВОТО (§9.3) -----------------

@app.route('/admin/management-reviews')
@role_required(['admin', 'worker', 'quality_control'])
def admin_management_reviews():
    """ISO 9001 §9.3 management review log - viewable by any staff role;
    only admin/quality_control may create/edit/delete - see the mutating
    routes below."""
    reviews = ManagementReview.query.order_by(ManagementReview.review_date.desc()).all()
    return render_template('admin_management_review.html', reviews=reviews, active_page='admin_management_reviews')


@app.route('/admin/management-reviews/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_management_review():
    review_date_raw = request.form.get('review_date', '').strip()
    if not review_date_raw:
        flash('Моля въведете дата на прегледа.', 'danger')
        return redirect(url_for('admin_management_reviews'))
    try:
        review_date = datetime.strptime(review_date_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_management_reviews'))

    # Snapshot the live QMS data at the moment the review is opened - see
    # ManagementReview's docstring for why this isn't recomputed later.
    open_capas = CapaRecord.query.filter(CapaRecord.status.in_(['open', 'in_progress'])).all()
    latest_audit = (
        InternalAudit.query.filter(InternalAudit.actual_date.isnot(None))
        .order_by(InternalAudit.actual_date.desc()).first()
    )
    audit_nonconformity_count = (
        AuditFinding.query.filter_by(audit_id=latest_audit.id, finding_type='nonconformity').count()
        if latest_audit else 0
    )

    review = ManagementReview(
        review_date=review_date,
        participants=request.form.get('participants', '').strip() or None,
        snapshot_open_capa_count=len(open_capas),
        snapshot_overdue_capa_count=sum(1 for c in open_capas if c.is_overdue),
        snapshot_audit_nonconformity_count=audit_nonconformity_count,
        snapshot_overdue_calibration_count=sum(
            1 for i in MeasuringInstrument.query.all() if i.calibration_status == 'overdue'
        ),
        created_by_id=current_user.id,
    )
    db.session.add(review)
    db.session.commit()
    log_action(f'Създаден преглед от ръководството за {review.review_date.strftime("%d.%m.%Y")}')
    flash('Прегледът беше създаден.', 'success')
    return redirect(url_for('admin_edit_management_review', review_id=review.id))


@app.route('/admin/management-reviews/<int:review_id>/edit')
@role_required(['admin', 'worker', 'quality_control'])
def admin_edit_management_review(review_id):
    review = ManagementReview.query.get_or_404(review_id)
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_management_review_edit.html', review=review, users=users, active_page='admin_management_reviews'
    )


@app.route('/admin/management-reviews/<int:review_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_management_review(review_id):
    review = ManagementReview.query.get_or_404(review_id)
    review_date_raw = request.form.get('review_date', '').strip()
    if not review_date_raw:
        flash('Моля въведете дата на прегледа.', 'danger')
        return redirect(url_for('admin_edit_management_review', review_id=review_id))
    try:
        review.review_date = datetime.strptime(review_date_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_edit_management_review', review_id=review_id))

    review.participants = request.form.get('participants', '').strip() or None
    review.customer_feedback = request.form.get('customer_feedback', '').strip() or None
    review.process_performance = request.form.get('process_performance', '').strip() or None
    review.resource_adequacy = request.form.get('resource_adequacy', '').strip() or None
    review.external_internal_changes = request.form.get('external_internal_changes', '').strip() or None
    review.risk_opportunity_actions = request.form.get('risk_opportunity_actions', '').strip() or None
    review.conclusion = request.form.get('conclusion', '').strip() or None
    db.session.commit()
    log_action(f'Обновен преглед от ръководството от {review.review_date.strftime("%d.%m.%Y")}')
    flash('Прегледът беше обновен.', 'success')
    return redirect(url_for('admin_edit_management_review', review_id=review_id))


@app.route('/admin/management-reviews/<int:review_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_management_review(review_id):
    review = ManagementReview.query.get_or_404(review_id)
    review_date_label = review.review_date.strftime('%d.%m.%Y')
    db.session.delete(review)
    db.session.commit()
    log_action(f'Изтрит преглед от ръководството от {review_date_label}')
    flash(f'Прегледът от {review_date_label} беше изтрит.', 'success')
    return redirect(url_for('admin_management_reviews'))


@app.route('/admin/management-reviews/<int:review_id>/actions/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_management_review_action(review_id):
    review = ManagementReview.query.get_or_404(review_id)
    description = request.form.get('description', '').strip()
    if not description:
        flash('Моля въведете описание на решението/действието.', 'danger')
        return redirect(url_for('admin_edit_management_review', review_id=review_id))

    responsible_id_raw = request.form.get('responsible_id', '')
    due_date = None
    due_date_raw = request.form.get('due_date', '').strip()
    if due_date_raw:
        try:
            due_date = datetime.strptime(due_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалиден срок.', 'danger')
            return redirect(url_for('admin_edit_management_review', review_id=review_id))

    db.session.add(ManagementReviewAction(
        review_id=review.id, description=description,
        responsible_id=int(responsible_id_raw) if responsible_id_raw.isdigit() else None,
        due_date=due_date,
    ))
    db.session.commit()
    log_action(f'Ново действие към преглед от ръководството от {review.review_date.strftime("%d.%m.%Y")}')
    flash('Действието беше добавено.', 'success')
    return redirect(url_for('admin_edit_management_review', review_id=review_id))


@app.route('/admin/management-reviews/actions/<int:action_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_management_review_action(action_id):
    action = ManagementReviewAction.query.get_or_404(action_id)
    review_id = action.review_id
    db.session.delete(action)
    db.session.commit()
    flash('Действието беше изтрито.', 'success')
    return redirect(url_for('admin_edit_management_review', review_id=review_id))


# ----------------- ISO 9001 - ОБУЧЕНИЕ НА ПЕРСОНАЛА (§7.2) -----------------

@app.route('/admin/training')
@role_required(['admin', 'worker', 'quality_control'])
def admin_training_records():
    """ISO 9001 §7.2 competence/training log - viewable by any staff role;
    only admin/quality_control may add/delete records - see the mutating
    routes below. Records are never edited once logged, same convention as
    InstrumentCalibrationRecord - a training either happened as recorded or
    gets deleted and re-logged, there's no partial-correction workflow."""
    employee_filter = request.args.get('employee_id', '')
    query = TrainingRecord.query
    if employee_filter.isdigit():
        query = query.filter_by(employee_id=int(employee_filter))
    records = query.order_by(TrainingRecord.training_date.desc()).all()
    employees = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_training.html', records=records, employees=employees, training_types=TRAINING_TYPES,
        employee_filter=employee_filter, active_page='admin_training'
    )


@app.route('/admin/training/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_training_record():
    employee_id_raw = request.form.get('employee_id', '')
    topic = request.form.get('topic', '').strip()
    training_date_raw = request.form.get('training_date', '').strip()
    if not employee_id_raw.isdigit() or not topic or not training_date_raw:
        flash('Моля попълнете служител, тема и дата на обучението.', 'danger')
        return redirect(url_for('admin_training_records'))

    try:
        training_date = datetime.strptime(training_date_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата на обучението.', 'danger')
        return redirect(url_for('admin_training_records'))

    valid_until = None
    valid_until_raw = request.form.get('valid_until', '').strip()
    if valid_until_raw:
        try:
            valid_until = datetime.strptime(valid_until_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна дата "валидно до".', 'danger')
            return redirect(url_for('admin_training_records'))

    training_type = request.form.get('training_type', 'other')
    if training_type not in TRAINING_TYPES:
        training_type = 'other'

    file = request.files.get('file')
    stored_filename = _save_upload(file, app.config['TRAINING_CERT_FOLDER'])
    original_filename = sanitize_display_filename(file.filename) if stored_filename else None

    employee = db.session.get(User, int(employee_id_raw))
    training = TrainingRecord(
        employee_id=int(employee_id_raw), topic=topic, training_type=training_type,
        trainer_name=request.form.get('trainer_name', '').strip() or None,
        training_date=training_date, valid_until=valid_until,
        effectiveness_evaluation=request.form.get('effectiveness_evaluation', '').strip() or None,
        notes=request.form.get('notes', '').strip() or None,
        filename=stored_filename, original_filename=original_filename, recorded_by_id=current_user.id,
    )
    db.session.add(training)
    db.session.commit()
    log_action(f'Записано обучение "{topic}" за {employee.username if employee else "?"}')
    flash('Обучението беше записано.', 'success')
    return redirect(url_for('admin_training_records'))


@app.route('/admin/training/<int:record_id>/download')
@role_required(['admin', 'worker', 'quality_control'])
def admin_download_training_certificate(record_id):
    record = TrainingRecord.query.get_or_404(record_id)
    if not record.filename:
        flash('Този запис няма прикачен сертификат.', 'danger')
        return redirect(url_for('admin_training_records'))
    return send_from_directory(
        app.config['TRAINING_CERT_FOLDER'], record.filename,
        as_attachment=True, download_name=record.original_filename
    )


@app.route('/admin/training/<int:record_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_training_record(record_id):
    record = TrainingRecord.query.get_or_404(record_id)
    if record.filename:
        path = os.path.join(app.config['TRAINING_CERT_FOLDER'], record.filename)
        if os.path.exists(path):
            os.remove(path)
    topic = record.topic
    db.session.delete(record)
    db.session.commit()
    log_action(f'Изтрит запис за обучение "{topic}"')
    flash('Записът за обучение беше изтрит.', 'success')
    return redirect(url_for('admin_training_records'))


# ----------------- ISO 9001 - РЕГИСТЪР НА РИСКА (§6.1) -----------------

@app.route('/admin/risk-register')
@role_required(['admin', 'worker', 'quality_control'])
def admin_risk_register():
    """ISO 9001 §6.1 risk/opportunity register - viewable by any staff role;
    only admin/quality_control may create/edit/delete - see the mutating
    routes below."""
    status_filter = request.args.get('status', '')
    query = RiskRegisterEntry.query
    if status_filter in RISK_STATUSES:
        query = query.filter_by(status=status_filter)
    entries = query.order_by(RiskRegisterEntry.created_at.desc()).all()
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_risk_register.html', entries=entries, users=users, risk_types=RISK_TYPES,
        categories=RISK_CATEGORIES, statuses=RISK_STATUSES, status_filter=status_filter,
        active_page='admin_risk_register'
    )


@app.route('/admin/risk-register/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_risk_entry():
    title = request.form.get('title', '').strip()
    identified_date_raw = request.form.get('identified_date', '').strip()
    likelihood_raw = request.form.get('likelihood', '')
    impact_raw = request.form.get('impact', '')

    if not title or not identified_date_raw or likelihood_raw not in '12345' or impact_raw not in '12345':
        flash('Моля попълнете заглавие, дата, вероятност и въздействие (1-5).', 'danger')
        return redirect(url_for('admin_risk_register'))

    try:
        identified_date = datetime.strptime(identified_date_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_risk_register'))

    review_date = None
    review_date_raw = request.form.get('review_date', '').strip()
    if review_date_raw:
        try:
            review_date = datetime.strptime(review_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна дата за преглед.', 'danger')
            return redirect(url_for('admin_risk_register'))

    risk_type = request.form.get('risk_type', 'risk')
    if risk_type not in RISK_TYPES:
        risk_type = 'risk'
    category = request.form.get('category', 'other')
    if category not in RISK_CATEGORIES:
        category = 'other'
    owner_id_raw = request.form.get('owner_id', '')

    entry = RiskRegisterEntry(
        title=title, description=request.form.get('description', '').strip() or None,
        risk_type=risk_type, category=category,
        likelihood=int(likelihood_raw), impact=int(impact_raw),
        owner_id=int(owner_id_raw) if owner_id_raw.isdigit() else None,
        identified_date=identified_date, review_date=review_date,
        created_by_id=current_user.id,
    )
    db.session.add(entry)
    db.session.commit()
    log_action(f'Добавен запис в регистъра на риска: "{title}"')
    flash('Записът беше добавен в регистъра на риска.', 'success')
    return redirect(url_for('admin_risk_register'))


@app.route('/admin/risk-register/<int:entry_id>/edit')
@role_required(['admin', 'worker', 'quality_control'])
def admin_edit_risk_entry(entry_id):
    entry = RiskRegisterEntry.query.get_or_404(entry_id)
    users = User.query.filter(User.role.in_(['admin', 'worker', 'quality_control'])).order_by(User.username).all()
    return render_template(
        'admin_risk_register_edit.html', entry=entry, users=users, risk_types=RISK_TYPES,
        categories=RISK_CATEGORIES, statuses=RISK_STATUSES, active_page='admin_risk_register'
    )


@app.route('/admin/risk-register/<int:entry_id>/update', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_update_risk_entry(entry_id):
    entry = RiskRegisterEntry.query.get_or_404(entry_id)
    title = request.form.get('title', '').strip()
    identified_date_raw = request.form.get('identified_date', '').strip()
    likelihood_raw = request.form.get('likelihood', '')
    impact_raw = request.form.get('impact', '')

    if not title or not identified_date_raw or likelihood_raw not in '12345' or impact_raw not in '12345':
        flash('Моля попълнете заглавие, дата, вероятност и въздействие (1-5).', 'danger')
        return redirect(url_for('admin_edit_risk_entry', entry_id=entry_id))

    try:
        entry.identified_date = datetime.strptime(identified_date_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_edit_risk_entry', entry_id=entry_id))

    review_date_raw = request.form.get('review_date', '').strip()
    if review_date_raw:
        try:
            entry.review_date = datetime.strptime(review_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Невалидна дата за преглед.', 'danger')
            return redirect(url_for('admin_edit_risk_entry', entry_id=entry_id))
    else:
        entry.review_date = None

    risk_type = request.form.get('risk_type', entry.risk_type)
    category = request.form.get('category', entry.category)
    status = request.form.get('status', entry.status)
    owner_id_raw = request.form.get('owner_id', '')

    entry.title = title
    entry.description = request.form.get('description', '').strip() or None
    entry.risk_type = risk_type if risk_type in RISK_TYPES else entry.risk_type
    entry.category = category if category in RISK_CATEGORIES else entry.category
    entry.likelihood = int(likelihood_raw)
    entry.impact = int(impact_raw)
    entry.owner_id = int(owner_id_raw) if owner_id_raw.isdigit() else None
    entry.mitigation_action = request.form.get('mitigation_action', '').strip() or None
    entry.status = status if status in RISK_STATUSES else entry.status

    db.session.commit()
    log_action(f'Обновен запис в регистъра на риска: "{entry.title}"')
    flash('Записът беше обновен.', 'success')
    return redirect(url_for('admin_edit_risk_entry', entry_id=entry_id))


@app.route('/admin/risk-register/<int:entry_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_risk_entry(entry_id):
    entry = RiskRegisterEntry.query.get_or_404(entry_id)
    title = entry.title
    db.session.delete(entry)
    db.session.commit()
    log_action(f'Изтрит запис от регистъра на риска: "{title}"')
    flash(f'Записът "{title}" беше изтрит.', 'success')
    return redirect(url_for('admin_risk_register'))


# ----------------- ISO 9001 - УДОВЛЕТВОРЕНОСТ НА КЛИЕНТИ (§9.1.2) -----------------

@app.route('/admin/customer-satisfaction')
@role_required(['admin', 'worker', 'quality_control'])
def admin_customer_satisfaction():
    """ISO 9001 §9.1.2 customer perception monitoring log - viewable by any
    staff role; only admin/quality_control may add/delete records - see the
    mutating routes below. Records are never edited once logged, same
    convention as TrainingRecord/InstrumentCalibrationRecord."""
    client_filter = request.args.get('client_id', '')
    query = CustomerSatisfactionRecord.query
    if client_filter.isdigit():
        query = query.filter_by(client_id=int(client_filter))
    records = query.order_by(CustomerSatisfactionRecord.feedback_date.desc()).all()
    clients = Client.query.order_by(Client.name).all()
    return render_template(
        'admin_customer_satisfaction.html', records=records, clients=clients,
        sources=SATISFACTION_SOURCES, client_filter=client_filter, active_page='admin_customer_satisfaction'
    )


@app.route('/admin/customer-satisfaction/create', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_add_satisfaction_record():
    client_id_raw = request.form.get('client_id', '')
    rating_raw = request.form.get('rating', '')
    feedback_date_raw = request.form.get('feedback_date', '').strip()

    if not client_id_raw.isdigit() or rating_raw not in '12345' or not feedback_date_raw:
        flash('Моля изберете клиент, оценка (1-5) и дата.', 'danger')
        return redirect(url_for('admin_customer_satisfaction'))

    try:
        feedback_date = datetime.strptime(feedback_date_raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Невалидна дата.', 'danger')
        return redirect(url_for('admin_customer_satisfaction'))

    source = request.form.get('source', 'survey')
    if source not in SATISFACTION_SOURCES:
        source = 'survey'

    client = db.session.get(Client, int(client_id_raw))
    record = CustomerSatisfactionRecord(
        client_id=int(client_id_raw), source=source, rating=int(rating_raw),
        comment=request.form.get('comment', '').strip() or None,
        order_reference=request.form.get('order_reference', '').strip() or None,
        feedback_date=feedback_date, recorded_by_id=current_user.id,
    )
    db.session.add(record)
    db.session.commit()
    log_action(f'Записана обратна връзка от клиент "{client.name if client else "?"}" ({record.rating}/5)')
    flash('Обратната връзка беше записана.', 'success')
    return redirect(url_for('admin_customer_satisfaction'))


@app.route('/admin/customer-satisfaction/<int:record_id>/delete', methods=['POST'])
@role_required(['admin', 'quality_control'])
def admin_delete_satisfaction_record(record_id):
    record = CustomerSatisfactionRecord.query.get_or_404(record_id)
    db.session.delete(record)
    db.session.commit()
    log_action('Изтрит запис за удовлетвореност на клиент')
    flash('Записът беше изтрит.', 'success')
    return redirect(url_for('admin_customer_satisfaction'))


# ----------------- QUICK-CREATE API ENDPOINTS -----------------

@app.route('/api/quick-create-detail', methods=['POST'])
@login_required
def api_quick_create_detail():
    """AJAX endpoint: create a Detail from a DXF file + material, returns JSON."""
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Нямате достъп.'}), 403

    name = request.form.get('name', '').strip()
    material_key = request.form.get('material', '')
    service_id_raw = request.form.get('service_id', '')
    service_id = int(service_id_raw) if service_id_raw and service_id_raw.isdigit() else None

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

    mat = MaterialPrice.query.filter_by(key=material_key).first()
    if not mat:
        return jsonify({'status': 'error', 'message': 'Невалиден избор на материал.'}), 400

    # A DXF is optional - mirrors admin_add_detail()'s bare-bones fallback
    # (manual price, width/height/total_length/pierce_count left at 0), same
    # shape _find_or_create_delivery_target already creates for a
    # delivery-note "new detail" line with no DXF.
    file = request.files.get('file')
    has_dxf = bool(file and file.filename)

    if has_dxf and not file.filename.lower().endswith('.dxf'):
        return jsonify({'status': 'error', 'message': 'Невалиден формат! Приемат се само .dxf файлове.'}), 400

    # Picking a cutting service is optional here - unlike calculated_price
    # (material only, see calculate_material_price()), cutting cost isn't
    # required to price a Detail at all. If a length-priced service IS
    # picked, _add_cutting_operation() auto-attaches a cutting Operation for
    # it below; anything else (blank, or a time-priced service) just gets
    # recorded as cutting_service_id without an auto-created Operation.
    if service_id and not db.session.get(Service, service_id):
        return jsonify({'status': 'error', 'message': 'Невалиден избор на услуга.'}), 400

    manual_price = None
    if not has_dxf:
        try:
            manual_price = float(request.form.get('manual_price', ''))
        except ValueError:
            return jsonify({'status': 'error', 'message': 'Моля качете .dxf файл или въведете цена на детайла ръчно.'}), 400
        if manual_price < 0:
            return jsonify({'status': 'error', 'message': 'Цената не може да бъде отрицателна.'}), 400

    pdf_file = request.files.get('pdf_file')
    if pdf_file and pdf_file.filename and not pdf_file.filename.lower().endswith('.pdf'):
        return jsonify({'status': 'error', 'message': 'Невалиден формат за референтен файл! Приемат се само .pdf файлове.'}), 400

    temp_path = None
    try:
        if has_dxf:
            filename = secure_filename(file.filename)
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
            file.save(temp_path)

            width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(temp_path)
            if width is None:
                return jsonify({'status': 'error', 'message': 'Грешка при обработката на DXF файла.'}), 400

            price = calculate_material_price(width, height, material_key)
            new_detail = Detail(
                name=name, material_key=material_key, width=width, height=height,
                total_length=total_length, pierce_count=pierce_count,
                calculated_price=price, geometry_json=json.dumps(shapes),
                erp_number=erp_number, code_number=code_number, cutting_service_id=service_id
            )
        else:
            new_detail = Detail(
                name=name, material_key=material_key, width=0.0, height=0.0,
                total_length=0.0, pierce_count=0,
                calculated_price=manual_price, geometry_json=None,
                erp_number=erp_number, code_number=code_number, cutting_service_id=service_id
            )
        db.session.add(new_detail)
        db.session.flush()  # assigns new_detail.id for the optional DetailDxfFile/Operation below
        if has_dxf:
            _add_cutting_operation(new_detail, service_id, total_length)
        if pdf_file and pdf_file.filename:
            pdf_stored_filename = _save_upload(pdf_file, app.config['DETAIL_DXF_FOLDER'], allowed_extensions={'pdf'})
            db.session.add(DetailDxfFile(
                detail_id=new_detail.id, filename=pdf_stored_filename,
                original_filename=sanitize_display_filename(pdf_file.filename), uploaded_by_id=current_user.id
            ))
        db.session.commit()
        log_action(f'Създаден детайл "{name}" (бърз избор, материал "{mat.display_name}", цена {new_detail.calculated_price:g} лв.)')

        return jsonify({
            'status': 'success',
            'detail': {
                'id': new_detail.id,
                'name': f"{new_detail.name} ({mat.display_name})",
                'price': new_detail.total_price
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
    component_lines = [f'{Detail.query.get(detail_id).name}: {quantity} бр.' for detail_id, quantity in components.items()]
    log_action(f'Създаден продукт "{name}" (бърз избор, {len(components)} детайл(а) в BOM)',
               details=f'Продукт "{name}"' + ('\n' + '\n'.join(component_lines) if component_lines else ''))

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

    material_type = _parse_material_type(request.form)
    brand = request.form.get('brand', '').strip() or None
    # Rods/profiles are cut to length on a saw, never pierced or DXF-cut -
    # no cutting/drill speed for either.
    skip_speed_fields = material_type in ('rods', 'profiles')

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        cutting_speed_mm_per_min = None if skip_speed_fields else float(request.form.get('cutting_speed_mm_per_min', ''))
        # Entered as seconds per pierce (shop-floor friendly), stored as the
        # pierces/min rate the pricing formula (_service_time_cost) uses.
        pierce_time_sec = None if skip_speed_fields else float(request.form.get('pierce_time_sec', ''))
        sheet_length_mm, sheet_width_mm, thickness_mm, height_mm = _parse_sheet_dimensions(request.form)
        price_per_kg_m2 = _parse_optional_float(request.form, 'price_per_kg_m2')
        price_per_kg_m = _parse_optional_float(request.form, 'price_per_kg_m')
        weight_kg = _parse_optional_float(request.form, 'weight_kg')
        erp_number = _parse_erp_number(request.form)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Всички цени, размери и ERP № трябва да бъдат валидни числа.'}), 400

    if cost_per_m2 < 0 or (cutting_speed_mm_per_min is not None and cutting_speed_mm_per_min <= 0) \
            or (pierce_time_sec is not None and pierce_time_sec <= 0):
        return jsonify({'status': 'error', 'message': 'Цената не може да бъде отрицателна, а скоростта на рязане/времето за пробождане трябва да бъдат положителни числа.'}), 400
    pierce_rate_per_min = 60.0 / pierce_time_sec if pierce_time_sec else None

    # Only a byte-for-byte resubmit (double click) is rejected - a difference
    # in any property (e.g. thickness) always makes a distinct catalog row,
    # even under the same name/brand.
    if _material_variant_exists(display_name, material_type, brand, round(cost_per_m2, 2),
                                 round(cutting_speed_mm_per_min, 2) if cutting_speed_mm_per_min is not None else None,
                                 round(pierce_rate_per_min, 2) if pierce_rate_per_min is not None else None,
                                 sheet_length_mm, sheet_width_mm, thickness_mm, height_mm):
        return jsonify({'status': 'error', 'message': f'Вече съществува идентичен материал с име "{display_name}".'}), 400

    conflict = _erp_number_conflict(erp_number)
    if conflict:
        return jsonify({'status': 'error', 'message': f'ERP № {erp_number} вече се използва от {conflict}.'}), 400

    new_material = MaterialPrice(
        key='pending',
        display_name=display_name,
        cost_per_m2=round(cost_per_m2, 2),
        cutting_speed_mm_per_min=round(cutting_speed_mm_per_min, 2) if cutting_speed_mm_per_min is not None else None,
        pierce_rate_per_min=round(pierce_rate_per_min, 2) if pierce_rate_per_min is not None else None,
        sheet_length_mm=sheet_length_mm,
        sheet_width_mm=sheet_width_mm,
        thickness_mm=thickness_mm,
        height_mm=height_mm,
        price_per_kg_m2=round(price_per_kg_m2, 2) if price_per_kg_m2 is not None else None,
        price_per_kg_m=round(price_per_kg_m, 2) if price_per_kg_m is not None else None,
        weight_kg=round(weight_kg, 2) if weight_kg is not None else None,
        erp_number=erp_number,
        code_number=request.form.get('code_number', '').strip() or None,
        type=material_type,
        brand=brand
    )
    db.session.add(new_material)
    db.session.flush()
    new_material.key = f'material_{new_material.id}'
    db.session.commit()
    log_action(f'Създаден материал "{display_name}" (бърз избор, цена {new_material.cost_per_m2:g} лв/м², тип {material_type})')

    return jsonify({
        'status': 'success',
        'material': {'key': new_material.key, 'option_text': format_material_option(new_material)}
    })


@app.route('/api/quick-create-instrument', methods=['POST'])
@login_required
def api_quick_create_instrument():
    """
    AJAX endpoint: create a MeasuringInstrument on the fly from the QC
    check form's per-measurement instrument picker (admin_quality_control.html),
    same pattern as api_quick_create_material - avoids leaving that form to
    go add one on the dedicated admin_measuring_instruments() page first.
    """
    if not current_user.can_manage_quality:
        return jsonify({'status': 'error', 'message': 'Нямате достъп.'}), 403

    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'Моля въведете име на инструмента.'}), 400

    try:
        accuracy_value = _parse_optional_float(request.form, 'accuracy_value')
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Точността трябва да бъде валидно число.'}), 400

    instrument = MeasuringInstrument(
        name=name,
        description=request.form.get('description', '').strip() or None,
        accuracy_value=accuracy_value,
        accuracy_unit=request.form.get('accuracy_unit', '').strip() or None,
    )
    db.session.add(instrument)
    db.session.commit()
    log_action(f'Създаден измервателен инструмент "{name}" (бърз избор)')

    return jsonify({
        'status': 'success',
        'instrument': {'id': instrument.id, 'option_text': instrument.display_label}
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
        return jsonify({'status': 'error', 'message': gettext('Моля въведете име на клиента.')}), 400

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
    log_action(f'Създаден клиент "{name}" (бърз избор)')

    return jsonify({'status': 'success', 'client': {'id': client.id, 'name': client.name}})


@app.route('/api/quick-create-deliverer', methods=['POST'])
@login_required
def api_quick_create_deliverer():
    """AJAX endpoint: create a Deliverer on the fly from the order-creation form. See api_quick_create_client()."""
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': gettext('Моля въведете име на куриера.')}), 400

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
    log_action(f'Създаден куриер "{name}" (бърз избор)')

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
    log_action(f'Машина за поръчка {order.order_number}: {machine_name or "-"}')

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
        log_action(f'Изтрит DXF файл "{filename}"')
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
        # Load-then-delete (not a bulk .delete() query) so the ORM also
        # clears each file's dxf_file_service association rows - a bulk
        # delete issues a raw DELETE FROM dxf_file and skips that, which
        # violates the FK when any file is linked to a Service.
        files = DxfFile.query.filter_by(user_id=current_user.id).all()
        deleted = len(files)
        for dxf_file in files:
            db.session.delete(dxf_file)
        db.session.commit()
        log_action(f'Изтрити {deleted} DXF файл(а) от личната библиотека')
        flash(f'Изтрити бяха {deleted} файл(а) от библиотеката.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване: {str(e)}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/machines/<int:id>/delete', methods=['POST'])
@role_required('admin')
def delete_machine(id):
    """
    Deletes a machine. Orders and DxfFiles that reference it keep existing
    (machine_id is nullable on both), so they're detached rather than
    deleted - a removed machine shouldn't take historical orders/uploads
    down with it. Any ShellyDevice meters linked to it (many-to-many, see
    shelly_device_machines) simply lose that one link and keep monitoring -
    removing a machine record was never a reason to stop watching a meter.
    Same treatment for any Service linked to it (service_machine) - the
    service just loses that one machine, other links/services are untouched.
    """
    machine = Machine.query.get_or_404(id)
    try:
        Order.query.filter_by(machine_id=machine.id).update({'machine_id': None})
        DxfFile.query.filter_by(machine_id=machine.id).update({'machine_id': None})
        db.session.execute(
            shelly_device_machines.delete().where(shelly_device_machines.c.machine_id == machine.id)
        )
        db.session.execute(
            service_machine.delete().where(service_machine.c.machine_id == machine.id)
        )
        machine_name = machine.name
        db.session.delete(machine)
        db.session.commit()
        log_action(f'Изтрита машина "{machine_name}"')
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
        material_name = material.display_name
        db.session.delete(material)
        db.session.commit()
        log_action(f'Изтрит материал "{material_name}"')
        flash(f'Материал "{material.display_name}" беше изтрит.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване: {str(e)}', 'danger')
    return redirect(url_for('admin_materials'))


# ----------------- SHELLY ENERGY MONITORING -----------------
# Live power monitoring off Shelly energy meters on the shop LAN. Two device
# generations are in use and their APIs don't overlap at all:
#   Gen2 (Shelly Pro 3EM)       GET /rpc/Shelly.GetStatus, em:0/em1:N components
#   Gen1 (older Shelly EM/3EM)  GET /status, an `emeters` list, no /rpc/ at all
# _shelly_get_status() below hides this split behind one call.
#
# READ-ONLY BY POLICY, not always by hardware: the Pro 3EM has no relay at all,
# so it physically cannot switch anything. The older Gen1 Shelly 3EM installed
# at 192.168.18.78 DOES have one onboard relay (`relays` in its /status) - but
# nothing in this app calls it. Turning a machine's power on/off remotely is a
# regulated safety question (EN 60204-1 / EN ISO 12100), not just a spare
# feature sitting on a device we happen to already own - don't wire up a
# Relay.Set/Switch.Set call without that conversation happening first.
#
# Meters are managed as the ShellyDevice DB table (add/remove from the
# /admin/power page itself, see admin_power_add_device()/
# admin_power_delete_device() near the routes below) - takes effect on the
# very next poll, no app restart. This replaced an env-var-only config
# (SHELLY_DEVICES) that made sense when there was exactly one meter and no
# UI to manage a list of them; seed_shelly_devices() migrates whatever was in
# that env var into the table once, on first startup after upgrading.
#
# There is still no per-machine *wiring* mapping beyond the label a device is
# given here: the Pro 3EM runs the 'triphase' profile, i.e. it measures a
# single 3-phase feed as a whole, not one machine per clamp. If a meter's
# clamps are ever rewired/reprofiled so one channel == one machine, that's a
# separate, later change (see the note further down on Machine.shelly_host).

def _parse_shelly_devices(raw):
    """
    'Табло 1=192.168.18.72,10.0.0.5' -> [('Табло 1', '192.168.18.72'), ('10.0.0.5', '10.0.0.5')].
    Only still used by seed_shelly_devices() below, to migrate the legacy
    SHELLY_DEVICES env var into the DB once - the app itself no longer reads
    this env var at request time.
    """
    devices = []
    for chunk in (raw or '').split(','):
        name, _, host = chunk.partition('=')
        name, host = name.strip(), host.strip()
        if not host:  # bare host, no label
            name, host = '', name
        if host:
            devices.append((name or host, host))
    return devices


def seed_shelly_devices():
    """
    One-time migration: if the ShellyDevice table is completely empty and the
    legacy SHELLY_DEVICES env var has entries, copy them in as the starting
    set. After that the table is the sole source of truth - machines are
    added/removed from /admin/power directly. Safe to call on every startup,
    same pattern as seed_material_prices(): a no-op the moment the table has
    any row.

    Caveat shared with seed_service_machine_cards(): if every device is later
    deleted through the UI and SHELLY_DEVICES is still set in the
    environment, the table looks empty again on the next restart and this
    reseeds from it. Unset SHELLY_DEVICES once machines are fully managed
    through the UI to avoid that.
    """
    if ShellyDevice.query.first():
        return
    for name, host in _parse_shelly_devices(os.environ.get('SHELLY_DEVICES')):
        db.session.add(ShellyDevice(name=name, host=host))
    db.session.commit()


SHELLY_TIMEOUT = 3.0


def shelly_rpc(host, method, params=None, timeout=SHELLY_TIMEOUT):
    """
    Call any Gen2 Shelly RPC method: GET http://<host>/rpc/<Method>?<params>.
    Returns the parsed JSON; raises on any network/HTTP/parse failure (callers
    that must survive an offline meter catch it - see shelly_device_snapshot).

    One generic caller instead of a wrapper per method, because every Gen2
    method is the same shape. The ones that matter here:

        Shelly.GetStatus                every component at once (em/emdata,
                                        temperature, wifi) - one call, which is
                                        why the dashboard uses this and not
                                        EM.GetStatus + Temperature.GetStatus + ...
        Shelly.GetDeviceInfo            model, gen, fw, profile, auth_en
        EM.GetStatus       {'id': 0}    live measurements only, smaller payload
        EMData.GetStatus   {'id': 0}    cumulative kWh counters
        EMData.GetRecords  {'id': 0}    which time ranges have stored history;
                                        cheap, call before shelly_history()

    Params go on the query string, so scalars only - that covers every read
    method. Full method list: http://<host>/rpc/Shelly.ListMethods

    NOTE: read methods only, deliberately. The Pro 3EM has no relay and nothing
    in this app should ever call a Set*/switching method - see the header
    comment on this section.
    """
    # ponytail: no auth handling. The meters currently run with auth_en=false.
    # If a device password is set (Shelly.SetAuth - recommended), this needs
    # HTTP digest auth added here or every read starts coming back 401.
    url = f'http://{host}/rpc/{method}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


# host -> 'gen1' | 'gen2'. In-memory only (resets on app restart) - a device's
# generation never changes at runtime, so once a host answers we don't need to
# ask again. Without this, every poll of a Gen1 device would cost two requests
# (a Gen2 attempt that always 404s, then the real Gen1 one).
_shelly_gen_cache = {}


def _shelly_get_status_gen1(host, timeout):
    """Gen1 devices (older Shelly EM/3EM/1PM...) have no /rpc/ namespace at
    all - the equivalent of Shelly.GetStatus is a plain GET /status, with a
    completely different JSON shape (an `emeters` list, not em:0/em1:N)."""
    with urllib.request.urlopen(f'http://{host}/status', timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _shelly_convector_status(conv):
    """
    Live on/off state (+ power_w, if the relay reports it - a plain Shelly
    1/Plus 1 has no metering at all, a 1PM/Plus 1PM does) for one Convector,
    over whichever transport it's configured with (connection_type) - HTTP
    polled (same LAN approach as the energy meters above) or read from
    _mqtt_conv_state (populated by _handle_mqtt_convector_message() as
    messages arrive - see _mqtt_convector_snapshot()). Returns is_on=None
    (with an error string) rather than raising, same offline-tolerant shape
    as shelly_device_snapshot(), so one unreachable convector doesn't break
    the whole page's poll.
    """
    if conv.connection_type == 'mqtt':
        return _mqtt_convector_snapshot(conv)
    try:
        if conv.device_type == 'shelly_gen2':
            # Switch.GetStatus already includes apower (W) in the same
            # response for a metering-capable switch (Plus/Pro 1PM) - no
            # separate call needed, it's just absent on a plain Switch.
            status = shelly_rpc(conv.host, 'Switch.GetStatus', {'id': conv.relay_channel})
            return {'online': True, 'is_on': bool(status.get('output')),
                    'power_w': status.get('apower'), 'error': None}
        data = _shelly_get_status_gen1(conv.host, SHELLY_TIMEOUT)
        relays = data.get('relays') or []
        if conv.relay_channel >= len(relays):
            return {'online': False, 'is_on': None, 'power_w': None,
                    'error': f'Устройството няма реле №{conv.relay_channel}.'}
        # Gen1 metering (1PM) reports power in a separate `meters` list,
        # parallel to `relays` by index - a plain (non-PM) Shelly 1 simply
        # has no `meters` key at all, so power_w stays None rather than
        # guessing/defaulting to 0 (which would misleadingly claim "0W" for
        # a device that can't measure power at all).
        meters = data.get('meters') or []
        power_w = meters[conv.relay_channel].get('power') if conv.relay_channel < len(meters) else None
        return {'online': True, 'is_on': bool(relays[conv.relay_channel]['ison']), 'power_w': power_w, 'error': None}
    except Exception:
        return {'online': False, 'is_on': None, 'power_w': None, 'error': 'Устройството не отговаря.'}


def _shelly_convector_set(conv, turn_on):
    """
    Turns one Convector's relay on/off, over whichever transport it's
    configured with - the ONE place in this app that switches real
    hardware. See Convector's docstring for why this device class is
    exempt from the read-only-by-policy stance that covers Machines/meters
    (confirmed explicitly with the user, not assumed from the hardware
    alone). Raises on any network/HTTP failure (the MQTT path can't detect
    failure the same way - see _mqtt_publish()'s own caveat) - the caller
    (admin_convector_toggle()) turns an HTTP failure into a flash message
    instead of silently doing nothing.
    """
    if conv.connection_type == 'mqtt':
        if conv.device_type == 'shelly_gen2':
            _mqtt_publish(f'{conv.mqtt_topic}/command/switch:{conv.relay_channel}', 'on' if turn_on else 'off')
        else:
            _mqtt_publish(f'{conv.mqtt_topic}/relay/{conv.relay_channel}/command', 'on' if turn_on else 'off')
        return
    if conv.device_type == 'shelly_gen2':
        shelly_rpc(conv.host, 'Switch.Set', {'id': conv.relay_channel, 'on': 'true' if turn_on else 'false'})
    else:
        url = f'http://{conv.host}/relay/{conv.relay_channel}?turn={"on" if turn_on else "off"}'
        with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT) as resp:
            resp.read()


def _shelly_get_status(host, timeout=SHELLY_TIMEOUT):
    """
    Full status for either generation of Shelly device. A host seen for the
    first time this run (or since the last restart) tries Gen2 first and
    falls back to Gen1 on the 404 a Gen1 device gives for an unknown /rpc/
    path; the result is cached in _shelly_gen_cache so steady-state polling
    of a known Gen1 device costs one request, not two.
    """
    if _shelly_gen_cache.get(host) == 'gen1':
        return _shelly_get_status_gen1(host, timeout)
    try:
        status = shelly_rpc(host, 'Shelly.GetStatus', timeout=timeout)
        _shelly_gen_cache[host] = 'gen2'
        return status
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        status = _shelly_get_status_gen1(host, timeout)
        _shelly_gen_cache[host] = 'gen1'
        return status


# The meter serialises stored records from flash at roughly 55-60 records/sec,
# measured on the installed unit: 1h window = 1.5s, 8h = 9s, 24h = 25s,
# 3 days = 75s, and a 14-day window never completes. Hence: fetch a day at a
# time with a timeout sized for a day, not for a live status read.
SHELLY_HISTORY_CHUNK = 86400
SHELLY_HISTORY_TIMEOUT = 60.0


def _shelly_time_chunks(start_ts, end_ts, step=SHELLY_HISTORY_CHUNK):
    """[(start, end), ...] tiling start_ts..end_ts in step-sized pieces."""
    return [(t, min(t + step, end_ts)) for t in range(start_ts, end_ts, step)]


def _parse_shelly_csv(text):
    """
    CSV body from /emdata/<id>/data.csv -> list of dicts with numbers parsed.
    Rows with blank/unparseable cells keep their remaining fields rather than
    being dropped: a record written while the meter was rebooting is still
    worth its valid columns.
    """
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {}
        for key, value in raw.items():
            if not key or value in (None, ''):
                continue
            try:
                parsed = int(value) if key == 'timestamp' else float(value)
            except (TypeError, ValueError):
                continue
            # The meter writes "nan"/"inf" for undefined fields (e.g. power
            # factor at 0 A) - float() parses those without raising, but
            # json.dumps then emits bare NaN/Infinity tokens, which aren't
            # valid JSON and make JSON.parse() in the browser throw. Treat
            # them as unparseable, same as a blank cell.
            if isinstance(parsed, float) and not math.isfinite(parsed):
                continue
            row[key] = parsed
        if 'timestamp' in row:
            rows.append(row)
    return rows


def shelly_history(host, start_ts, end_ts, em_id=0):
    """
    Minute-resolution history straight off the meter - the device is the
    archive, so there's no local table to keep in sync.

    Each record holds ~50 fields: per phase the total/fundamental active
    energy, returned energy, lagging/leading reactive energy, and the
    max/min/avg of voltage, current, active power and apparent power over that
    minute, plus neutral current. The min/max spread within a minute is what
    lets you separate a cycling load (chiller, compressor) from a steady one.

    Two limits worth knowing before building on this:
      - retention is ~45-48 days rolling; older data is gone for good, so
        anything longer-term has to be copied off the meter into Postgres.
      - each day-chunk itself is slow (see SHELLY_HISTORY_CHUNK above) - the
        meter serialises its own flash at a fixed ~55-60 records/sec no
        matter how the request arrives, so this can't make one chunk faster.
        What it does fix is walls-of-chunks: fetching a multi-day window
        used to pay N * ~25s by doing chunks one after another; now every
        chunk is requested at once, so wall time is whichever single chunk
        is slowest. Don't call this from a request handler on a wide window
        without a loading state regardless.

    Uses the CSV endpoint rather than EMData.GetData because GetData paginates
    (returns next_record_ts and needs a loop), while the CSV route returns the
    whole window in one response.
    """
    chunks = _shelly_time_chunks(start_ts, end_ts)
    # ponytail: capped at 4 concurrent connections - a Shelly's HTTP server is
    # a small embedded stack, not sized for a big fan-out. Raise this (or drop
    # the cap) once it's confirmed the real device handles more without
    # errors/slowdown.
    with ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as pool:
        results = pool.map(lambda chunk: _shelly_history_chunk(host, em_id, chunk), chunks)
        rows = []
        for chunk_rows in results:
            rows.extend(chunk_rows)
    return rows


def _shelly_history_chunk(host, em_id, chunk):
    chunk_start, chunk_end = chunk
    url = (f'http://{host}/emdata/{em_id}/data.csv'
           f'?ts={chunk_start}&end_ts={chunk_end}&add_keys=true')
    with urllib.request.urlopen(url, timeout=SHELLY_HISTORY_TIMEOUT) as resp:
        return _parse_shelly_csv(resp.read().decode('utf-8'))
#daad

def _shelly_readings(status):
    """
    Flatten a full-status payload into channels the dashboard can render,
    handling three shapes so neither a profile switch on a Gen2 meter nor a
    mixed Gen1/Gen2 fleet breaks this page:
      Gen2 'triphase'  -> one `em:0` component = three phases of ONE 3-phase feed
      Gen2 'monophase' -> up to three independent `em1:N` meters = one per circuit
      Gen1 (EM/3EM)    -> an `emeters` list, one dict per channel, no /rpc/ at all
    Returns (channels, total_act_power_W, total_energy_kWh). Missing keys read
    as None/0 rather than raising - a meter mid-reboot returns partial payloads.
    """
    channels = []
    total_power = 0.0
    total_energy = 0.0

    # Key presence, not truthiness: a meter mid-reboot sends "em:0": {} (or
    # null), which is still a triphase device and must not fall through to the
    # monophase branch and silently render zero channels.
    if 'em:0' in status:  # Gen2 triphase profile
        em = status.get('em:0') or {}
        for phase in ('a', 'b', 'c'):
            channels.append({
                'label': f'Фаза {phase.upper()}',
                'voltage': em.get(f'{phase}_voltage'),
                'current': em.get(f'{phase}_current'),
                'act_power': em.get(f'{phase}_act_power'),
                'aprt_power': em.get(f'{phase}_aprt_power'),
                'pf': em.get(f'{phase}_pf'),
                'freq': em.get(f'{phase}_freq'),
            })
        total_power = em.get('total_act_power') or 0.0
        total_energy = ((status.get('emdata:0') or {}).get('total_act') or 0.0) / 1000.0
    elif 'emeters' in status:  # Gen1 (Shelly EM / 3EM)
        emeters = status.get('emeters') or []
        # A 3-channel Gen1 device is always a 3EM measuring one 3-phase feed
        # (unlike Gen2, Gen1 has no separate monophase/triphase profile switch);
        # anything else (the 2-channel Shelly EM) is independent circuits.
        labels = [f'Фаза {c}' for c in 'ABC'] if len(emeters) == 3 else \
                 [f'Вход {i + 1}' for i in range(len(emeters))]
        for label, m in zip(labels, emeters):
            voltage, current = m.get('voltage'), m.get('current')
            channels.append({
                'label': label,
                'voltage': voltage,
                'current': current,
                'act_power': m.get('power'),
                # Gen1 doesn't report apparent power directly - S = V*I is its
                # definition, so derive it rather than leave it blank.
                'aprt_power': voltage * current if voltage is not None and current is not None else None,
                'pf': m.get('pf'),
                'freq': None,  # not exposed per-channel in Gen1's /status
            })
            total_power += m.get('power') or 0.0
            # Gen1 energy counters are Watt-minutes, not Wh - /60000 for kWh
            # (Gen2's total_act above is already Wh, hence /1000 there instead).
            total_energy += (m.get('total') or 0.0) / 60000.0
        # Prefer the device's own aggregate over our re-summed one when present.
        total_power = status.get('total_power', total_power)
    else:  # Gen2 monophase profile
        for i in range(3):
            meter = status.get(f'em1:{i}')
            if not meter:
                continue
            channels.append({
                'label': f'Вход {i + 1}',
                'voltage': meter.get('voltage'),
                'current': meter.get('current'),
                'act_power': meter.get('act_power'),
                'aprt_power': meter.get('aprt_power'),
                'pf': meter.get('pf'),
                'freq': meter.get('freq'),
            })
            total_power += meter.get('act_power') or 0.0
            energy = (status.get(f'em1data:{i}') or {}).get('total_act_energy') or 0.0
            total_energy += energy / 1000.0

    return channels, round(total_power, 1), round(total_energy, 2)


# ----------------- MQTT LIVE FEED -----------------
# Alternative to HTTP polling for Shelly meters that publish readings to an
# MQTT broker (see ShellyDevice.mqtt_topic). Observed real topic shapes on
# this shop's broker (both Gen1-style flat scalar-per-topic publishing, one
# with the default "shellies/<id>" root and one reconfigured to a bare
# custom prefix with no "shellies/" at all):
#   <prefix>/online                      "true" / "false"
#   <prefix>/relay/<N>                   "on" / "off"
#   <prefix>/emeter/<N>/power             "2535.08"      (W)
#   <prefix>/emeter/<N>/voltage           "232.51"       (V)
#   <prefix>/emeter/<N>/current           "14.03"        (A)
#   <prefix>/emeter/<N>/pf                "0.77"
#   <prefix>/emeter/<N>/total             "106592.0"     (Watt-minutes, Gen1 units)
#   <prefix>/emeter/<N>/total_returned    "18686.2"
#   <prefix>/announce, <prefix>/info      JSON metadata (not needed for live power)
# Temperature/humidity sensors (TemperatureSensor.mqtt_topic) come in
# several incompatible shapes depending on TEMP_SENSOR_TYPES - unlike the
# power side above, these do NOT all self-describe from the topic alone, so
# sensor_type picks which one _handle_mqtt_temp_message() parses with:
#   'shelly_ht_gen1' (classic Shelly H&T, flat scalar per topic):
#     <prefix>/online                       "true" / "false"
#     <prefix>/sensor/temperature           "21.30"   (deg C)
#     <prefix>/sensor/humidity              "45.00"   (%)
#     <prefix>/sensor/battery               "98"      (%)
#   'shelly_gen2' (Shelly Plus/Pro H&T, JSON per component - same dual
#   direct-status/events-rpc shape as the Gen2+ power side above):
#     <prefix>/status/temperature:0         {"id":0,"tC":21.3,"tF":70.3}
#     <prefix>/status/humidity:0            {"id":0,"rh":45.0}
#     <prefix>/status/devicepower:0         {"id":0,"battery":{"percent":87}}
#     <prefix>/events/rpc                   NotifyStatus wrapping any of the above under "params"
#   'generic_flat' (DIY/ESPHome/Tasmota-with-custom-topic, no "sensor/"
#   segment - the simplest common convention for these):
#     <prefix>/temperature                  "21.30"
#     <prefix>/humidity                     "45.00"
#     <prefix>/battery                      "98"
# `mqtt_topic` is exactly the prefix as configured on the device (whatever
# was typed into the add-device form) - messages are matched by
# `topic.startswith(mqtt_topic + '/')` regardless of type, then
# sensor_type decides how the leaf under that prefix gets parsed.
#
# Convectors (Convector.connection_type='mqtt') are the ONE thing in this
# section the app also PUBLISHES to, not just subscribes - see
# _mqtt_publish()/_shelly_convector_set(). Status still comes in the usual
# subscribe-and-parse way, by device_type (CONVECTOR_TYPES):
#   'shelly_gen1':
#     <prefix>/online                       "true" / "false"
#     <prefix>/relay/<N>                    "on" / "off"           (status)
#     <prefix>/relay/<N>/command            "on" / "off"           (PUBLISHED to switch it)
#   'shelly_gen2' (same dual direct-status/events-rpc shape as elsewhere):
#     <prefix>/status/switch:<N>            {"id":<N>,"output":true}   (status)
#     <prefix>/events/rpc                   NotifyStatus wrapping the above under "params"
#     <prefix>/command/switch:<N>           "on" / "off"           (PUBLISHED - Shelly's
#                                            documented simple per-component MQTT command
#                                            channel; NOT yet confirmed against a live Gen2
#                                            device on this broker - if it doesn't switch
#                                            anything, the documented fallback is a full
#                                            JSON-RPC Switch.Set request published to
#                                            <prefix>/rpc instead).

_mqtt_state = {}
_mqtt_temp_state = {}
_mqtt_conv_state = {}
_mqtt_lock = threading.Lock()
_mqtt_client_instance = None


def _handle_mqtt_message(topic, payload):
    """Dispatches one incoming message to _mqtt_state (power, ShellyDevice)
    or _mqtt_temp_state (TemperatureSensor), whichever's configured topic
    prefixes it matches. Prefixes are looked up fresh per message (cheap, a
    handful of rows) rather than cached at subscribe time, so adding a new
    device's topic takes effect without restarting the listener."""
    with app.app_context():
        try:
            power_prefixes = [t for (t,) in db.session.query(ShellyDevice.mqtt_topic)
                               .filter(ShellyDevice.mqtt_topic.isnot(None)).all()]
            temp_sensors = db.session.query(TemperatureSensor.mqtt_topic, TemperatureSensor.sensor_type).all()
            conv_sensors = db.session.query(Convector.mqtt_topic, Convector.device_type) \
                .filter(Convector.connection_type == 'mqtt', Convector.mqtt_topic.isnot(None)).all()
        finally:
            db.session.remove()

    prefix = next((p for p in power_prefixes if topic == f'{p}/online' or topic.startswith(f'{p}/emeter/')
                   or topic.startswith(f'{p}/relay/') or topic.startswith(f'{p}/status/')
                   or topic == f'{p}/events/rpc'), None)
    if prefix:
        _handle_mqtt_power_message(prefix, topic[len(prefix) + 1:], payload)
        return

    # Any leaf under the prefix is passed through - sensor_type (not the
    # topic shape) decides which of them actually mean something, so a
    # temp sensor doesn't need its own per-type list of leaf patterns here.
    match = next(((p, st) for p, st in temp_sensors if topic == f'{p}/online' or topic.startswith(f'{p}/')), None)
    if match:
        prefix, sensor_type = match
        _handle_mqtt_temp_message(prefix, sensor_type, topic[len(prefix) + 1:], payload)
        return

    match = next(((p, dt) for p, dt in conv_sensors if topic == f'{p}/online' or topic.startswith(f'{p}/')), None)
    if match:
        prefix, device_type = match
        _handle_mqtt_convector_message(prefix, device_type, topic[len(prefix) + 1:], payload)


def _handle_mqtt_power_message(prefix, leaf, payload):
    with _mqtt_lock:
        state = _mqtt_state.setdefault(prefix, {'online': False, 'channels': {}, 'last_seen': None})
        state['last_seen'] = datetime.utcnow()

        if leaf == 'online':
            state['online'] = payload.strip().lower() == 'true'
            return

        # Gen1 (original Shelly EM/3EM): <prefix>/emeter/<idx>/<field>, one
        # scalar value per leaf topic.
        m = re.match(r'^emeter/(\d+)/(\w+)$', leaf)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            if field not in ('power', 'voltage', 'current', 'pf', 'total', 'total_returned'):
                return
            try:
                value = float(payload)
            except ValueError:
                return
            state['channels'].setdefault(idx, {})[field] = value
            # A device publishing emeter readings is implicitly online, even
            # before/without an explicit .../online message (some firmwares
            # only send that one on state change, not on every reading).
            state['online'] = True
            return

        # Gen2+ (Plus/Pro/Gen3, e.g. Shelly Pro 3EM): one JSON status object
        # per EM component instead of Gen1's one-scalar-per-topic - either
        # published directly (<prefix>/status/em:0, when "RPC status over
        # MQTT" is on) or wrapped in an RPC notification the device always
        # sends on a reading change (<prefix>/events/rpc). Field names are
        # from Shelly's published Gen2 RPC schema (stable across their
        # product line) but - unlike DTSU666_REGISTERS - not yet cross-
        # checked against a real Pro 3EM; if the numbers look wrong once one
        # is actually reporting, this is the first place to check.
        m = re.match(r'^status/(em:\d+|em1:\d+)$', leaf)
        if m:
            try:
                data = json.loads(payload)
            except ValueError:
                return
            _apply_gen2_em_status(state, m.group(1), data)
            return

        if leaf == 'events/rpc':
            try:
                envelope = json.loads(payload)
            except ValueError:
                return
            for key, data in (envelope.get('params') or {}).items():
                if isinstance(data, dict) and re.match(r'^em:\d+$|^em1:\d+$', key):
                    _apply_gen2_em_status(state, key, data)


def _apply_gen2_em_status(state, component_key, data):
    """Folds one Gen2+ 'em:N' (3-phase) or 'em1:N' (single-phase) status
    object into the same {idx: {power, voltage, current, pf}} channel shape
    _mqtt_snapshot() renders for Gen1 - see the caller's docstring for the
    empirical-confirmation caveat. Lifetime energy isn't included: Gen2
    exposes that via a separate 'emdata:N' component in Wh, not the
    Watt-minute 'total' Gen1 channels carry, so a Gen2-via-MQTT device
    currently always shows 0 kWh total rather than guessing at a
    conversion - only live power/voltage/current/pf are wired up here."""
    if component_key.startswith('em1:'):
        idx = int(component_key.split(':')[1])
        channel = state['channels'].setdefault(idx, {})
        for field, key in (('power', 'act_power'), ('voltage', 'voltage'), ('current', 'current'), ('pf', 'pf')):
            if key in data:
                channel[field] = data[key]
    else:
        for i, letter in enumerate('abc'):
            channel = state['channels'].setdefault(i, {})
            for field, key in (('power', f'{letter}_act_power'), ('voltage', f'{letter}_voltage'),
                               ('current', f'{letter}_current'), ('pf', f'{letter}_pf')):
                if key in data:
                    channel[field] = data[key]
    state['online'] = True


def _mqtt_snapshot(name, prefix):
    """Render-ready dict for one MQTT-backed device, same shape as
    shelly_device_snapshot()'s HTTP path - see that function's return value.
    A device with no cached state yet (broker just started, or it's never
    published) reads as offline rather than raising, same tolerance as an
    unreachable HTTP meter."""
    with _mqtt_lock:
        state = _mqtt_state.get(prefix)
        state = dict(state, channels=dict(state['channels'])) if state else None

    if not state or not state['online']:
        return {
            'name': name, 'host': prefix, 'online': False,
            'error': None if state else 'Няма получени данни по MQTT за този префикс още.',
            'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
            'temperature': None, 'rssi': None,
        }

    indices = sorted(state['channels'].keys())
    labels = [f'Фаза {c}' for c in 'ABC'] if len(indices) == 3 else [f'Вход {i + 1}' for i in indices]
    channels = []
    total_power = 0.0
    total_energy = 0.0
    for label, idx in zip(labels, indices):
        c = state['channels'][idx]
        voltage, current = c.get('voltage'), c.get('current')
        channels.append({
            'label': label, 'voltage': voltage, 'current': current,
            'act_power': c.get('power'),
            'aprt_power': voltage * current if voltage is not None and current is not None else None,
            'pf': c.get('pf'), 'freq': None,
        })
        total_power += c.get('power') or 0.0
        # Gen1 energy counters are Watt-minutes, not Wh - /60000 for kWh,
        # same conversion as _shelly_readings()'s HTTP Gen1 branch.
        total_energy += (c.get('total') or 0.0) / 60000.0

    return {
        'name': name, 'host': prefix, 'online': True, 'error': None,
        'channels': channels, 'total_power': round(total_power, 1), 'total_energy': round(total_energy, 2),
        'temperature': None, 'rssi': None,
    }


def _handle_mqtt_temp_message(prefix, sensor_type, leaf, payload):
    with _mqtt_lock:
        state = _mqtt_temp_state.setdefault(
            prefix, {'online': False, 'temperature': None, 'humidity': None, 'battery': None, 'last_seen': None}
        )
        state['last_seen'] = datetime.utcnow()

        if leaf == 'online':
            state['online'] = payload.strip().lower() == 'true'
            return

        if sensor_type == 'shelly_gen2':
            _apply_gen2_temp_leaf(state, leaf, payload)
            return

        # 'shelly_ht_gen1': <prefix>/sensor/<field>; 'generic_flat' (default
        # for anything else): <prefix>/<field> directly, no "sensor/" segment.
        leaf_pattern = r'^sensor/(temperature|humidity|battery)$' if sensor_type == 'shelly_ht_gen1' \
            else r'^(temperature|humidity|battery)$'
        m = re.match(leaf_pattern, leaf)
        if not m:
            return
        try:
            value = float(payload)
        except ValueError:
            return
        state[m.group(1)] = value
        # Same reasoning as the power side: a reading implies the sensor is
        # awake and online, even without/before an explicit .../online message.
        state['online'] = True


def _apply_gen2_temp_leaf(state, leaf, payload):
    """Shelly Plus/Pro H&T (Gen2+) publishes JSON per component, either
    directly (<prefix>/status/temperature:0 etc.) or wrapped in a
    <prefix>/events/rpc NotifyStatus envelope - same dual-path shape as
    _apply_gen2_em_status() for the 3EM Pro, so a device with "RPC status
    over MQTT" off still works via the events/rpc notifications it always
    sends on a reading change."""
    def apply_component(key, data):
        if not isinstance(data, dict):
            return
        if key.startswith('temperature:') and 'tC' in data:
            state['temperature'] = data['tC']
            state['online'] = True
        elif key.startswith('humidity:') and 'rh' in data:
            state['humidity'] = data['rh']
            state['online'] = True
        elif key.startswith('devicepower:'):
            battery = (data.get('battery') or {}).get('percent')
            if battery is not None:
                state['battery'] = battery
                state['online'] = True

    m = re.match(r'^status/(temperature:\d+|humidity:\d+|devicepower:\d+)$', leaf)
    if m:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        apply_component(m.group(1), data)
        return

    if leaf == 'events/rpc':
        try:
            envelope = json.loads(payload)
        except ValueError:
            return
        for key, data in (envelope.get('params') or {}).items():
            if re.match(r'^(temperature|humidity|devicepower):\d+$', key):
                apply_component(key, data)


def _mqtt_temp_snapshot(sensor):
    """Render-ready dict for one TemperatureSensor - reads as offline (not
    raising) if the broker hasn't delivered anything yet, same tolerance as
    _mqtt_snapshot().

    'online' here is the sensor's own last-published LWT state, not "do we
    have a value to show" - a battery/deep-sleep sensor (Shelly H&T-style)
    is EXPECTED to publish online=false between wake cycles (could be
    10-15+ minutes apart), which is normal operation, not a fault. So
    unlike _mqtt_snapshot()'s power devices, going offline here does NOT
    blank temperature/humidity/battery/last_seen back to None - they always
    reflect the last value actually received, and only stay None if nothing
    has ever come in for this sensor at all. The UI uses 'online' just to
    dim/mark the reading as not-currently-live, never to hide it."""
    with _mqtt_lock:
        state = _mqtt_temp_state.get(sensor.mqtt_topic)
        state = dict(state) if state else None

    if not state:
        return {
            'name': sensor.name, 'online': False,
            'error': 'Няма получени данни по MQTT за този сензор още.',
            'temperature': None, 'humidity': None, 'battery': None, 'last_seen': None,
        }
    return {
        'name': sensor.name, 'online': state['online'], 'error': None,
        'temperature': state['temperature'], 'humidity': state['humidity'], 'battery': state['battery'],
        'last_seen': state['last_seen'].strftime('%H:%M:%S') if state['last_seen'] else None,
    }


def _handle_mqtt_convector_message(prefix, device_type, leaf, payload):
    """Tracks relay state PER CHANNEL INDEX (not just the one this specific
    Convector row cares about) in _mqtt_conv_state, same as the power side
    keeps a {idx: {...}} channels dict - a multi-relay device sharing one
    prefix across several Convector rows just works without this function
    needing per-row context. Each channel is {'is_on', 'power_w'} - power_w
    stays None for a plain (non-PM) Shelly 1/Plus 1, which never publishes
    a power reading at all."""
    with _mqtt_lock:
        state = _mqtt_conv_state.setdefault(prefix, {'online': False, 'channels': {}, 'last_seen': None})
        state['last_seen'] = datetime.utcnow()

        if leaf == 'online':
            state['online'] = payload.strip().lower() == 'true'
            return

        if device_type == 'shelly_gen2':
            _apply_gen2_switch_leaf(state, leaf, payload)
            return

        m = re.match(r'^relay/(\d+)$', leaf)
        if m:
            idx = int(m.group(1))
            channel = state['channels'].setdefault(idx, {'is_on': None, 'power_w': None})
            channel['is_on'] = payload.strip().lower() == 'on'
            state['online'] = True
            return

        # Gen1 1PM-only topic (mirrors <prefix>/emeter/<N>/power on the
        # energy-meter side) - simply never arrives for a non-metering relay.
        m = re.match(r'^relay/(\d+)/power$', leaf)
        if m:
            try:
                value = float(payload)
            except ValueError:
                return
            channel = state['channels'].setdefault(int(m.group(1)), {'is_on': None, 'power_w': None})
            channel['power_w'] = value
            state['online'] = True


def _apply_gen2_switch_leaf(state, leaf, payload):
    """Shelly Plus/Pro (Gen2+) relay status - same dual direct-status/
    events-rpc shape as _apply_gen2_temp_leaf()/_apply_gen2_em_status().
    A metering-capable switch (Plus/Pro 1PM) includes 'apower' in the SAME
    status object as 'output', so both land in one pass; a plain switch's
    status simply never has that key, leaving power_w at None."""
    def apply_component(key, data):
        if not (isinstance(data, dict) and key.startswith('switch:') and 'output' in data):
            return
        channel = state['channels'].setdefault(int(key.split(':')[1]), {'is_on': None, 'power_w': None})
        channel['is_on'] = bool(data['output'])
        if 'apower' in data:
            channel['power_w'] = data['apower']
        state['online'] = True

    m = re.match(r'^status/(switch:\d+)$', leaf)
    if m:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        apply_component(m.group(1), data)
        return

    if leaf == 'events/rpc':
        try:
            envelope = json.loads(payload)
        except ValueError:
            return
        for key, data in (envelope.get('params') or {}).items():
            if re.match(r'^switch:\d+$', key):
                apply_component(key, data)


def _mqtt_convector_snapshot(conv):
    """Render-ready {online, is_on, power_w, error} for one MQTT Convector -
    same offline-tolerant shape as _shelly_convector_status() (the HTTP
    path), so admin_convectors_data() doesn't need to care which transport
    a given row uses."""
    with _mqtt_lock:
        state = _mqtt_conv_state.get(conv.mqtt_topic)
        state = dict(state, channels=dict(state['channels'])) if state else None

    if not state or not state['online']:
        return {
            'online': False, 'is_on': None, 'power_w': None,
            'error': None if state else 'Няма получени данни по MQTT за този конвектор още.',
        }
    channel = state['channels'].get(conv.relay_channel)
    if not channel or channel['is_on'] is None:
        return {'online': False, 'is_on': None, 'power_w': None, 'error': f'Няма данни за реле №{conv.relay_channel} още.'}
    return {'online': True, 'is_on': channel['is_on'], 'power_w': channel.get('power_w'), 'error': None}


def _mqtt_publish(topic, payload):
    """Publishes one message to the broker - the ONE place this app's MQTT
    client sends anything rather than just listening (see
    _shelly_convector_set()). Silently does nothing if the broker isn't
    configured/connected (MQTT_BROKER_HOST unset, or connect_async() hasn't
    finished yet) - the caller in that case just gets a command that never
    arrives, same practical effect as any other unreachable-device failure."""
    if _mqtt_client_instance is not None:
        _mqtt_client_instance.publish(topic, payload)


def start_mqtt_listener():
    """
    Background MQTT subscriber - runs for as long as the app process does,
    updating _mqtt_state as readings arrive (paho's own network thread does
    the actual socket work; loop_start() just launches it). A no-op if
    MQTT_BROKER_HOST isn't set, same "optional, off by default" convention
    as SMTP_HOST/ANTHROPIC_API_KEY. Auto-reconnects on drop (paho's default
    behavior) - a shop Wi-Fi hiccup shouldn't need an app restart to recover.
    """
    global _mqtt_client_instance
    host = os.environ.get('MQTT_BROKER_HOST')
    if not host:
        return
    port = int(os.environ.get('MQTT_BROKER_PORT', '1883'))

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe('#')

    def on_message(client, userdata, msg):
        try:
            _handle_mqtt_message(msg.topic, msg.payload.decode('utf-8', errors='replace'))
        except Exception:
            pass  # one malformed message shouldn't kill the listener

    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    username = os.environ.get('MQTT_USERNAME')
    if username:
        client.username_pw_set(username, os.environ.get('MQTT_PASSWORD', ''))
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(host, port, keepalive=30)
    client.loop_start()
    _mqtt_client_instance = client


def shelly_device_snapshot(name, host, mqtt_topic=None, connection_type='ip'):
    """
    Snapshot one meter via whichever transport connection_type selects (see
    CONNECTION_TYPES): MQTT from _mqtt_snapshot()'s cache, or HTTP-polling
    `host` (either generation - see _shelly_get_status) for 'ip'. 'udp_rpc'/
    'coiot' aren't implemented yet - see ShellyDevice.connection_type's
    docstring for why - and report a clear "not implemented" offline state
    rather than silently falling back to another transport, which would hide
    a device that's actually misconfigured. Never raises otherwise: an
    unreachable/silent meter is a normal state on a shop floor (Wi-Fi drop,
    panel powered down), and one dead device must not blank out the whole
    dashboard.
    """
    if connection_type == 'mqtt':
        return _mqtt_snapshot(name, mqtt_topic)
    if connection_type in ('udp_rpc', 'coiot'):
        label = CONNECTION_TYPES.get(connection_type, connection_type)
        return {
            'name': name, 'host': host or mqtt_topic, 'online': False,
            'error': f'Връзка "{label}" все още не е реализирана в приложението.',
            'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
            'temperature': None, 'rssi': None,
        }
    try:
        status = _shelly_get_status(host)
    except Exception as e:
        # Include the exception type, not just str(e): a flaky/weak-signal
        # meter (common once mounted inside a metal DIN panel, which
        # attenuates Wi-Fi badly) can drop the connection mid-response and
        # raise something like http.client.BadStatusLine whose str() is just
        # the raw malformed bytes it choked on (e.g. "1D") - unreadable
        # without knowing what kind of failure that was.
        return {
            'name': name, 'host': host, 'online': False,
            'error': f'{type(e).__name__}: {e}',
            'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
            'temperature': None, 'rssi': None,
        }

    channels, total_power, total_energy = _shelly_readings(status)
    if 'emeters' in status:  # Gen1: different key paths, no onboard temp sensor exposed
        temperature = None
        rssi = (status.get('wifi_sta') or {}).get('rssi')
    else:  # Gen2
        temperature = (status.get('temperature:0') or {}).get('tC')
        rssi = (status.get('wifi') or {}).get('rssi')

    return {
        'name': name, 'host': host, 'online': True, 'error': None,
        'channels': channels,
        'total_power': total_power,
        'total_energy': total_energy,
        'temperature': temperature,
        'rssi': rssi,
    }


def shelly_fleet_snapshot(devices):
    """
    Poll every configured meter at once rather than one-by-one, so one
    unreachable meter's SHELLY_TIMEOUT doesn't serialize onto every other
    meter's read - without this, N meters cost up to N * SHELLY_TIMEOUT per
    page refresh; with it, the wall time is whichever single read is slowest.
    Order of the returned list always matches `devices`, regardless of which
    thread finishes first. Plain ThreadPoolExecutor: these are blocking network
    reads, not CPU work, so the GIL is a non-issue here (an MQTT-backed device
    in the same batch is just an in-memory dict read - see _mqtt_snapshot -
    so it costs nothing to include, no separate code path needed here).

    `devices` is (name, host, mqtt_topic) triples - see _shelly_snapshot_args().
    """
    if not devices:
        return []
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        return list(pool.map(lambda d: shelly_device_snapshot(*d), devices))


def _shelly_snapshot_args(devices):
    """ShellyDevice rows -> (name, host, mqtt_topic, connection_type) tuples for shelly_fleet_snapshot()."""
    return [(d.name, d.host, d.mqtt_topic, d.connection_type) for d in devices]


SHELLY_POLLER_INTERVAL = 60  # seconds - matches Gen2's own minute-resolution history
_shelly_poller_started = False


def _shelly_history_poll_tick():
    """
    One poll-and-log cycle: snapshot every configured meter, write a
    ShellyReadingLog row for each one online. Split out from
    start_shelly_history_poller() as a standalone function so a test can
    call it directly (inside an app context) without spinning up the real
    background thread/sleep loop.
    """
    devices = ShellyDevice.query.order_by(ShellyDevice.id).all()
    if not devices:
        return
    snapshots = shelly_fleet_snapshot(_shelly_snapshot_args(devices))
    now_ts = int(datetime.now().timestamp())
    for snap in snapshots:
        if snap['online']:
            db.session.add(ShellyReadingLog(
                host=snap['host'], ts=now_ts,
                total_power=snap['total_power'], total_energy=snap['total_energy'],
                channels_json=json.dumps(snap['channels']),
            ))
    db.session.commit()


def start_shelly_history_poller(interval=SHELLY_POLLER_INTERVAL):
    """
    Real always-on replacement for logging readings only while someone had
    /admin/power open: a daemon thread that runs _shelly_history_poll_tick()
    on a fixed interval for as long as the app process runs. Reuses
    shelly_fleet_snapshot() - the same Gen1/Gen2-normalized poll the live
    dashboard already does - so this needs no per-generation history API and
    works identically for both.

    Call once, from a real entrypoint (app.py's __main__ block, wsgi.py) -
    deliberately not at module import time, so `import app` in tests never
    spins up a thread that hits real device hosts. Idempotent: a second call
    in the same process is a no-op.

    ponytail: single global thread, no cross-process dedup - if this is ever
    run behind more than one gunicorn/waitress worker, each worker starts
    its own thread and every reading gets logged once per worker (same
    caveat this codebase already accepts for flask-limiter's in-memory
    storage - see requirements.txt notes). Fine for one worker; add a lock
    (e.g. a Postgres advisory lock keyed by host) if that changes.
    """
    global _shelly_poller_started
    if _shelly_poller_started:
        return
    _shelly_poller_started = True

    def _loop():
        while True:
            try:
                with app.app_context():
                    try:
                        _shelly_history_poll_tick()
                    finally:
                        db.session.remove()
            except Exception:
                pass  # one bad tick (meter/DB hiccup) shouldn't kill the poller
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name='shelly-history-poller').start()


SOLIS_POLLER_INTERVAL = 60  # seconds - same cadence as ShellyReadingLog
_solis_poller_started = False


def _solis_history_poll_tick():
    """One poll-and-log cycle for every configured Solis inverter - same
    split-out-for-testability shape as _shelly_history_poll_tick(). Logs the
    full snapshot (see SolisReadingLog), skipping a device that's currently
    unreachable rather than writing a blank row for it."""
    devices = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all()
    if not devices:
        return
    now_ts = int(datetime.now().timestamp())
    for device in devices:
        snap = _solis_snapshot(device)
        if not snap['online']:
            continue
        db.session.add(SolisReadingLog(
            device_id=device.id, ts=now_ts,
            ac_power=snap['total_power'], pv_power=snap['pv']['power'],
            battery_soc=snap['battery']['soc'], battery_power=snap['battery']['power'],
            temperature=snap['temperature'], battery_temperature=snap['battery']['temperature'],
            battery_fault_bits=snap['battery']['fault_bits'], snapshot_json=json.dumps(snap),
        ))
    db.session.commit()


def start_solis_history_poller(interval=SOLIS_POLLER_INTERVAL):
    """Real always-on Solis equivalent of start_shelly_history_poller() -
    same idempotent-daemon-thread shape, see that function's docstring for
    the reasoning (single-worker assumption, reloader guard at the call
    site, etc.)."""
    global _solis_poller_started
    if _solis_poller_started:
        return
    _solis_poller_started = True

    def _loop():
        while True:
            try:
                with app.app_context():
                    try:
                        _solis_history_poll_tick()
                    finally:
                        db.session.remove()
            except Exception:
                pass  # one bad tick (inverter/DB hiccup) shouldn't kill the poller
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name='solis-history-poller').start()


@app.route('/admin/power')
@role_required('admin')
def admin_power():
    """
    Live power-consumption dashboard for the shop's Shelly energy meters, plus
    the add/remove-a-machine management panel (see admin_power_add_device()/
    admin_power_delete_device()). The page is mostly static chrome -
    admin_power_data() below feeds the live cards on an interval; the
    management panel is server-rendered from `devices` directly, so an
    add/delete takes a normal full-page redirect back here rather than going
    through the JS polling path.

    ?host=<ip> scopes the page to one machine (clicking a machine's name in the
    all-machines view links here with it set) - same template, same JS, just a
    single-device payload instead of the whole fleet. Not validated against
    what's configured: an unknown host simply matches nothing and renders an
    empty page, same as a fleet with zero configured meters.
    """
    devices = ShellyDevice.query.order_by(ShellyDevice.id).all()
    modbus_devices = ModbusDevice.query.order_by(ModbusDevice.id).all()
    machines = Machine.query.order_by(Machine.name).all()
    panels = ElectricalPanel.query.join(Room).order_by(Room.name, ElectricalPanel.name).all()
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    # Just the name lookup for renderBatteryGroup()'s "БМС порт N" labels -
    # live data itself already comes from each Solis device's own
    # battery_groups (see _solis_snapshot()), this only attaches a friendly
    # Cabinet/Stack name where one's been configured.
    battery_stacks = BatteryStack.query.filter(
        BatteryStack.source_type == 'inverter',
        BatteryStack.inverter_device_id.isnot(None), BatteryStack.bms_port.isnot(None),
    ).all()
    focus_host = request.args.get('host') or None
    # Modbus devices have no separate "host" identity of their own - their
    # snapshot/history key is "host:port" (see _dtsu666_snapshot()), so a
    # focus link to one of them looks like "192.168.18.90:502" instead of a
    # bare IP. "История за период" only exists for Shelly meters (Gen1/Gen2 -
    # see admin_power_history()) - a Modbus meter has no logging table to
    # query, so the template only offers that section when the focused
    # device really is a Shelly one.
    focus_modbus_host = lambda d: f'{d.host}:{d.port}'
    focus_name = next((d.name for d in devices if d.host == focus_host), None)
    focus_is_shelly = focus_name is not None
    if focus_name is None:
        focus_name = next((d.name for d in modbus_devices if focus_modbus_host(d) == focus_host), focus_host)
    # Every device has a working "История за период" now: Gen2 goes through
    # shelly_history() as before, Gen1 through _aggregate_local_shelly_log()
    # (our own ShellyReadingLog, fed by the always-on poller - see
    # start_shelly_history_poller()) instead of the meter's own history API.
    return render_template('admin_power.html', devices=devices, modbus_devices=modbus_devices,
                           machines=machines, panels=panels, rooms=rooms, battery_stacks=battery_stacks,
                           connection_types=CONNECTION_TYPES, modbus_device_types=MODBUS_DEVICE_TYPES,
                           active_page='admin_power', focus_host=focus_host, focus_name=focus_name,
                           focus_is_shelly=focus_is_shelly)


def _parse_machine_ids(form):
    """
    Multi-select/checkbox field 'machine_ids' -> a de-duplicated list of int
    Machine ids, in the order submitted, ignoring blank/non-numeric entries.
    No entries selected is a normal, valid "no machine linked yet" state,
    not an error - returns []. Shared by _selected_machines() (lenient - see
    its docstring) and the power-device routes below, which pair this with
    _resolve_machines_or_none() for strict validation instead.
    """
    ids = []
    for raw in form.getlist('machine_ids'):
        raw = (raw or '').strip()
        if raw.isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    return ids


def _resolve_machines_or_none(machine_ids):
    """Look up every id at once; returns None (not an empty list) if any id doesn't exist."""
    if not machine_ids:
        return []
    found = Machine.query.filter(Machine.id.in_(machine_ids)).all()
    return found if len(found) == len(machine_ids) else None


@app.route('/admin/power/devices/add', methods=['POST'])
@role_required('admin')
def admin_power_add_device():
    """
    Add a machine to the power dashboard. Takes effect on the very next poll
    (2s)/MQTT message - no app restart, unlike the old SHELLY_DEVICES env var
    this replaced. A label + at least one of IP/MQTT topic + any number of
    linked Machines; nothing about the meter's generation needs declaring,
    _shelly_get_status()/the MQTT topic shape figures that out on contact.
    """
    name = request.form.get('name', '').strip()
    host = request.form.get('host', '').strip()
    host = re.sub(r'^https?://', '', host).rstrip('/') if host else ''  # tolerate a pasted URL
    mqtt_topic = request.form.get('mqtt_topic', '').strip().strip('/')
    connection_type = request.form.get('connection_type', 'ip')
    if connection_type not in CONNECTION_TYPES:
        connection_type = 'ip'
    if connection_type == 'ip' and not host:
        flash('Моля въведете IP адрес за връзка тип "IP (HTTP)".', 'danger')
        return redirect(url_for('admin_power'))
    if connection_type == 'mqtt' and not mqtt_topic:
        flash('Моля въведете MQTT тема за връзка тип "MQTT".', 'danger')
        return redirect(url_for('admin_power'))
    if host and ' ' in host:
        flash('Невалиден IP адрес.', 'danger')
        return redirect(url_for('admin_power'))
    if host and ShellyDevice.query.filter_by(host=host).first():
        flash(f'Вече има добавена машина с адрес "{host}".', 'danger')
        return redirect(url_for('admin_power'))
    if mqtt_topic and ShellyDevice.query.filter_by(mqtt_topic=mqtt_topic).first():
        flash(f'Вече има добавена машина с MQTT тема "{mqtt_topic}".', 'danger')
        return redirect(url_for('admin_power'))
    machines = _resolve_machines_or_none(_parse_machine_ids(request.form))
    if machines is None:
        flash('Една от избраните машини не съществува.', 'danger')
        return redirect(url_for('admin_power'))
    panel_id_raw = request.form.get('panel_id', '')
    panel_id = int(panel_id_raw) if panel_id_raw.isdigit() and db.session.get(ElectricalPanel, int(panel_id_raw)) else None
    display_name = name or host or mqtt_topic
    device = ShellyDevice(
        name=display_name, host=host or None, mqtt_topic=mqtt_topic or None,
        connection_type=connection_type, machines=machines, panel_id=panel_id,
    )
    db.session.add(device)
    db.session.commit()
    log_action(f'Добавен електромер "{display_name}"')
    flash(f'Машината "{display_name}" беше добавена.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/power/devices/<int:device_id>/rename', methods=['POST'])
@role_required('admin')
def admin_power_rename_device(device_id):
    """Edit a device's full connection details (name/IP/MQTT topic/panel) -
    machines/history stay put otherwise (see admin_power_set_device_machines()
    for the linked-machines checklist, a separate form/route)."""
    device = ShellyDevice.query.get_or_404(device_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Името не може да бъде празно.', 'danger')
        return redirect(url_for('admin_power'))

    host = request.form.get('host', '').strip()
    host = re.sub(r'^https?://', '', host).rstrip('/') if host else ''  # tolerate a pasted URL
    mqtt_topic = request.form.get('mqtt_topic', '').strip().strip('/')
    connection_type = request.form.get('connection_type', device.connection_type)
    if connection_type not in CONNECTION_TYPES:
        connection_type = device.connection_type
    if connection_type == 'ip' and not host:
        flash('Моля въведете IP адрес за връзка тип "IP (HTTP)".', 'danger')
        return redirect(url_for('admin_power'))
    if connection_type == 'mqtt' and not mqtt_topic:
        flash('Моля въведете MQTT тема за връзка тип "MQTT".', 'danger')
        return redirect(url_for('admin_power'))
    if host and ' ' in host:
        flash('Невалиден IP адрес.', 'danger')
        return redirect(url_for('admin_power'))
    if host and ShellyDevice.query.filter(ShellyDevice.host == host, ShellyDevice.id != device.id).first():
        flash(f'Вече има друга машина с адрес "{host}".', 'danger')
        return redirect(url_for('admin_power'))
    if mqtt_topic and ShellyDevice.query.filter(ShellyDevice.mqtt_topic == mqtt_topic, ShellyDevice.id != device.id).first():
        flash(f'Вече има друга машина с MQTT тема "{mqtt_topic}".', 'danger')
        return redirect(url_for('admin_power'))

    old_name = device.name
    device.name = name
    device.host = host or None
    device.mqtt_topic = mqtt_topic or None
    device.connection_type = connection_type
    panel_id_raw = request.form.get('panel_id', '')
    device.panel_id = int(panel_id_raw) if panel_id_raw.isdigit() and db.session.get(ElectricalPanel, int(panel_id_raw)) else None
    db.session.commit()
    log_action(f'Редактиран електромер "{old_name}" → "{name}"')
    flash(f'Машината "{name}" беше обновена.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/power/devices/<int:device_id>/delete', methods=['POST'])
@role_required('admin')
def admin_power_delete_device(device_id):
    """Remove a machine from the power dashboard - stops polling it immediately."""
    device = ShellyDevice.query.get_or_404(device_id)
    name = device.name
    db.session.delete(device)
    db.session.commit()
    log_action(f'Изтрит електромер "{name}"')
    flash(f'Машината "{device.name}" беше премахната от таблото.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/power/devices/<int:device_id>/set-machines', methods=['POST'])
@role_required('admin')
def admin_power_set_device_machines(device_id):
    """
    Replace the full set of Machines an already-added meter is linked to
    (none selected = fully unlinked) - separate from admin_power_add_device()
    since the link is often only confirmed after the meter's already been
    added and someone has physically checked which machine(s) its clamps are
    actually on. One meter can link to several machines at once (a shared
    feed/sub-panel) and one machine can have several meters (see
    shelly_device_machines) - this route doesn't need to special-case either.
    """
    device = ShellyDevice.query.get_or_404(device_id)
    machines = _resolve_machines_or_none(_parse_machine_ids(request.form))
    if machines is None:
        flash('Една от избраните машини не съществува.', 'danger')
        return redirect(url_for('admin_power'))
    device.machines = machines
    db.session.commit()
    log_action(f'Обновени машини за електромер "{device.name}": ' + (', '.join(m.name for m in machines) if machines else 'няма'))
    flash(f'Връзките за "{device.name}" бяха обновени.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/power/data')
@role_required('admin')
def admin_power_data():
    """
    JSON feed polled by admin_power.html. Polling happens server-side so the
    meters only ever have to be reachable from the app host, not from every
    admin's browser.

    ?host=<ip> limits the poll to that one meter - the single-machine view
    has no use for the other meters' readings, so there's no reason to poll
    them every 2s just to throw the result away client-side.
    """
    host = request.args.get('host')
    shelly_rows = ShellyDevice.query.filter_by(host=host).all() if host else ShellyDevice.query.order_by(ShellyDevice.id).all()
    snapshots = shelly_fleet_snapshot(_shelly_snapshot_args(shelly_rows))
    for snap in snapshots:
        snap['kind'] = 'shelly'

    # Modbus meters join the same feed (_dtsu666_snapshot() already returns
    # the same shape shelly_device_snapshot() does - see its docstring), keyed
    # by "host:port" since that's the only identity a Modbus device has (no
    # bare host column). Polled in-line (not thread-pooled like the Shelly
    # fleet) - same simple sequential approach _collect_power_aggregates()
    # already uses, since register reads are already serialized per-device
    # anyway (see _modbus_lock_for()).
    modbus_rows = ModbusDevice.query.order_by(ModbusDevice.id).all()
    if host:
        modbus_rows = [d for d in modbus_rows if f'{d.host}:{d.port}' == host]
    # 'solis_grid_meter' rows are virtual - no Modbus connection of their
    # own (see ModbusDevice.source_device_id) - resolved in a second pass
    # below from the real rows' already-fetched snapshots, rather than
    # polling the same registers a second time.
    real_rows = [d for d in modbus_rows if d.device_type != 'solis_grid_meter']
    real_snapshots = [
        _solis_snapshot(d) if d.device_type == 'solis_s6' else _dtsu666_snapshot(d)
        for d in real_rows
    ]
    snap_by_device_id = {}
    for device, snap in zip(real_rows, real_snapshots):
        snap['kind'] = 'solis' if device.device_type == 'solis_s6' else 'modbus'
        snap_by_device_id[device.id] = snap
    modbus_snapshots = []
    for device in modbus_rows:
        if device.device_type == 'solis_grid_meter':
            snap = _solis_grid_meter_view_snapshot(device, snap_by_device_id.get(device.source_device_id))
            snap['kind'] = 'solis_grid_meter'
        else:
            snap = snap_by_device_id[device.id]
        modbus_snapshots.append(snap)

    rows = list(shelly_rows) + list(modbus_rows)
    snapshots = snapshots + modbus_snapshots
    # Join the Machine link in at the route layer rather than threading a
    # Machine dependency down into shelly_fleet_snapshot/shelly_device_snapshot -
    # those talk to meters and shouldn't need to know the catalog exists.
    # Zipped by position since both snapshot lists preserve their `rows`' order.
    for device, snap in zip(rows, snapshots):
        snap['machines'] = [{
            'name': m.name, 'status': m.status,
            'last_maintenance': m.last_maintenance.strftime('%d.%m.%Y %H:%M') if m.last_maintenance else None,
        } for m in device.machines]

    return jsonify({
        'ts': datetime.now().strftime('%H:%M:%S'),
        'devices': snapshots,
    })


def _aggregate_shelly_history(rows):
    """
    Collapse shelly_history() rows into one summary: arithmetic mean for every
    field, except *_total_act_energy fields, which are summed into one kWh
    figure instead. Those fields are each record's energy consumed during
    that one minute (not a running counter) - see shelly_history()'s
    docstring - so summing them across the window gives exactly the same
    number as (ending cumulative reading - starting cumulative reading)
    would, without needing to know the counter's value at two exact instants.

    Per-channel fields (e.g. 'a_avg_voltage') are additionally grouped by
    their prefix so the caller can render one card per phase, same shape as
    the live dashboard's channel cards.
    """
    if not rows:
        return {'count': 0, 'energy_kwh': 0.0, 'channels': {}}

    energy_wh = 0.0
    sums, counts = {}, {}
    for row in rows:
        for key, value in row.items():
            if key == 'timestamp':
                continue
            if key.endswith('_total_act_energy'):
                energy_wh += value
                continue
            sums[key] = sums.get(key, 0.0) + value
            counts[key] = counts.get(key, 0) + 1

    channels = {}
    for key, total in sums.items():
        mean = total / counts[key]
        prefix, sep, rest = key.partition('_')
        if sep:
            channels.setdefault(prefix, {})[rest] = mean

    return {
        'count': len(rows),
        'energy_kwh': round(energy_wh / 1000.0, 2),
        'channels': channels,
    }


# Position in a device's channels[] -> the a/b/c/n keys renderHistoryChannel()
# on the frontend already knows how to label (CHANNEL_LABELS in
# admin_power.html). Matches how _shelly_readings() orders a Gen1 3EM's
# phases (labels = Фаза A/B/C in that order); a device with more than 3
# channels (e.g. a 2-input Gen1 EM has only 2, never more) just has its
# extra ones dropped from the local-log history view.
_LOCAL_LOG_CHANNEL_KEYS = ('a', 'b', 'c')


def _aggregate_local_shelly_log(host, start_ts, end_ts):
    """
    Fallback history source for Gen1 meters, which have no working
    shelly_history() (see its docstring - the Gen2-only /emdata/ API 404s on
    Gen1 firmware, and the real Gen1 endpoint is unmapped). Reads
    ShellyReadingLog instead: same shape of answer (record count, period kWh,
    per-phase avg voltage/current + min/max power) as _aggregate_shelly_history()
    produces for Gen2, just built from our own poll-tick log's channels_json
    instead of the meter's own history - renderHistoryChannel() on the
    frontend doesn't need to know or care which source it came from.

    Coarser than the Gen2 path in one way: only covers time since the
    background poller started running (see ShellyReadingLog's docstring),
    not a full replacement for real device-side history - just what's
    actually on hand.
    """
    rows = (ShellyReadingLog.query
            .filter(ShellyReadingLog.host == host,
                    ShellyReadingLog.ts >= start_ts,
                    ShellyReadingLog.ts < end_ts)
            .order_by(ShellyReadingLog.ts)
            .all())
    if not rows:
        return {'count': 0, 'energy_kwh': 0.0, 'channels': {}}
    first_energy, last_energy = rows[0].total_energy, rows[-1].total_energy
    energy_kwh = round(last_energy - first_energy, 2) if None not in (first_energy, last_energy) else 0.0

    sums, counts, mins, maxs = {}, {}, {}, {}
    for row in rows:
        if not row.channels_json:
            continue  # rows logged before channels_json existed
        for i, ch in enumerate(json.loads(row.channels_json)):
            if i >= len(_LOCAL_LOG_CHANNEL_KEYS):
                break
            key = _LOCAL_LOG_CHANNEL_KEYS[i]
            for field in ('voltage', 'current'):
                v = ch.get(field)
                if v is not None:
                    sums[(key, field)] = sums.get((key, field), 0.0) + v
                    counts[(key, field)] = counts.get((key, field), 0) + 1
            p = ch.get('act_power')
            if p is not None:
                mins[(key, 'act_power')] = min(mins.get((key, 'act_power'), p), p)
                maxs[(key, 'act_power')] = max(maxs.get((key, 'act_power'), p), p)
            a = ch.get('aprt_power')
            if a is not None:
                maxs[(key, 'aprt_power')] = max(maxs.get((key, 'aprt_power'), a), a)

    channels = {}
    for key in _LOCAL_LOG_CHANNEL_KEYS:
        if (key, 'voltage') not in counts and (key, 'act_power') not in mins:
            continue
        channels[key] = {
            'avg_voltage': sums[(key, 'voltage')] / counts[(key, 'voltage')] if (key, 'voltage') in counts else None,
            'avg_current': sums[(key, 'current')] / counts[(key, 'current')] if (key, 'current') in counts else None,
            'min_act_power': mins.get((key, 'act_power')),
            'max_act_power': maxs.get((key, 'act_power')),
            'max_aprt_power': maxs.get((key, 'aprt_power')),
        }

    return {'count': len(rows), 'energy_kwh': max(energy_kwh, 0.0), 'channels': channels}


@app.route('/admin/power/history')
@role_required('admin')
def admin_power_history():
    """
    On-demand historical aggregation for one meter over [start_ts, end_ts)
    (Unix seconds) - see _aggregate_shelly_history() for the mean-vs-sum
    split. shelly_history() only works against Gen2 (see docs/SHELLY_API.md),
    so a known-Gen1 host is routed to _aggregate_local_shelly_log() instead -
    our own poll-tick log - rather than 404ing against the meter directly.
    """
    host = request.args.get('host', '')
    try:
        start_ts = int(request.args.get('start_ts'))
        end_ts = int(request.args.get('end_ts'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Невалиден период.'}), 400
    if end_ts <= start_ts:
        return jsonify({'error': 'Крайната дата трябва да е след началната.'}), 400
    if not ShellyDevice.query.filter_by(host=host).first():
        return jsonify({'error': 'Няма такава машина.'}), 404
    if _shelly_gen_cache.get(host) == 'gen1':
        # shelly_history() only speaks Gen2's /emdata/ API - a Gen1 device
        # (older Shelly EM/3EM) 404s on it, and the real Gen1 endpoint is
        # unmapped (see _aggregate_local_shelly_log()'s docstring). Serve
        # from our own poll-tick log instead of erroring out.
        return jsonify(_aggregate_local_shelly_log(host, start_ts, end_ts))

    try:
        rows = shelly_history(host, start_ts, end_ts)
        result = _aggregate_shelly_history(rows)
    except Exception as e:
        return jsonify({'error': f'Историята не е налична за това устройство ({type(e).__name__}: {e}).'}), 502

    return jsonify(result)


# ----------------- СГРАДИ / ПОМЕЩЕНИЯ -----------------

@app.route('/admin/buildings')
@role_required(['admin', 'worker'])
def admin_buildings():
    buildings = Building.query.order_by(Building.name).all()
    return render_template('admin_buildings.html', buildings=buildings, active_page='admin_buildings')


@app.route('/admin/buildings/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_building():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на сградата.', 'danger')
        return redirect(url_for('admin_buildings'))
    db.session.add(Building(name=name))
    db.session.commit()
    log_action(f'Добавена сграда "{name}"')
    flash(f'Сграда "{name}" беше добавена.', 'success')
    return redirect(url_for('admin_buildings'))


@app.route('/admin/buildings/<int:building_id>/rename', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_rename_building(building_id):
    building = Building.query.get_or_404(building_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Името не може да бъде празно.', 'danger')
        return redirect(url_for('admin_buildings'))
    building.name = name
    db.session.commit()
    flash('Сградата беше преименувана.', 'success')
    return redirect(url_for('admin_buildings'))


@app.route('/admin/buildings/<int:building_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_building(building_id):
    building = Building.query.get_or_404(building_id)
    if building.rooms:
        flash('Тази сграда все още има помещения - изтрийте ги първо.', 'danger')
        return redirect(url_for('admin_buildings'))
    name = building.name
    db.session.delete(building)
    db.session.commit()
    log_action(f'Изтрита сграда "{name}"')
    flash(f'Сграда "{name}" беше изтрита.', 'success')
    return redirect(url_for('admin_buildings'))


@app.route('/admin/buildings/<int:building_id>/rooms/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_room(building_id):
    building = Building.query.get_or_404(building_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на помещението.', 'danger')
        return redirect(url_for('admin_buildings'))
    db.session.add(Room(name=name, building_id=building.id))
    db.session.commit()
    log_action(f'Добавено помещение "{name}" в сграда "{building.name}"')
    flash(f'Помещение "{name}" беше добавено.', 'success')
    return redirect(url_for('admin_buildings'))


@app.route('/admin/rooms/<int:room_id>/rename', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_rename_room(room_id):
    room = Room.query.get_or_404(room_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Името не може да бъде празно.', 'danger')
        return redirect(url_for('admin_buildings'))
    room.name = name
    db.session.commit()
    flash('Помещението беше преименувано.', 'success')
    return redirect(url_for('admin_buildings'))


@app.route('/admin/rooms/<int:room_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_room(room_id):
    """
    Deleting a room un-places (not deletes) any Machine standing in it and
    removes its ElectricalPanels - panels are this app's own bookkeeping
    (nothing physical is lost by forgetting where a panel's icon was on the
    map), unlike Machines, which are the shop's real production catalog and
    must never disappear as a side effect of tidying up the map.
    """
    room = Room.query.get_or_404(room_id)
    room_name = room.name
    for machine in Machine.query.filter_by(room_id=room.id).all():
        machine.room_id = None
    for panel in ElectricalPanel.query.filter_by(room_id=room.id).all():
        for machine in Machine.query.filter_by(panel_id=panel.id).all():
            machine.panel_id = None
        for device in ShellyDevice.query.filter_by(panel_id=panel.id).all():
            device.panel_id = None
        for device in ModbusDevice.query.filter_by(panel_id=panel.id).all():
            device.panel_id = None
        for child in ElectricalPanel.query.filter_by(parent_panel_id=panel.id).all():
            child.parent_panel_id = None
        db.session.delete(panel)
    db.session.delete(room)
    db.session.commit()
    log_action(f'Изтрито помещение "{room_name}"')
    flash(f'Помещение "{room_name}" беше изтрито.', 'success')
    return redirect(url_for('admin_buildings'))


# ----------------- ЕЛЕКТРИЧЕСКИ ТАБЛА -----------------

@app.route('/admin/panels')
@role_required(['admin', 'worker'])
def admin_panels():
    panels = ElectricalPanel.query.join(Room).order_by(Room.name, ElectricalPanel.name).all()
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    return render_template('admin_panels.html', panels=panels, rooms=rooms, active_page='admin_panels')


@app.route('/admin/panels/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_panel():
    name = request.form.get('name', '').strip()
    room_id_raw = request.form.get('room_id', '')
    if not name or not room_id_raw.isdigit():
        flash('Моля въведете име и изберете помещение за таблото.', 'danger')
        return redirect(url_for('admin_panels'))
    room = db.session.get(Room, int(room_id_raw))
    if not room:
        flash('Избраното помещение не съществува.', 'danger')
        return redirect(url_for('admin_panels'))
    db.session.add(ElectricalPanel(name=name, room_id=room.id, notes=request.form.get('notes', '').strip() or None))
    db.session.commit()
    log_action(f'Добавено ел. табло "{name}" в помещение "{room.name}"')
    flash(f'Ел. табло "{name}" беше добавено.', 'success')
    return redirect(url_for('admin_panels'))


def _panel_descendants(panel):
    """Every panel (in)directly fed FROM `panel` - used to keep the
    "Захранва се от" choices cycle-free (see edit_panel_window()/
    admin_update_panel()): a panel can't be set to feed from itself or from
    anything already downstream of it."""
    for child in panel.child_panels:
        yield child
        yield from _panel_descendants(child)


@app.route('/admin/panels/<int:panel_id>/edit')
@role_required(['admin', 'worker'])
def edit_panel_window(panel_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/panels."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    # A panel can't feed from itself or from anything downstream of it (that
    # would be a cycle in the distribution tree) - excluded from the choices
    # rather than merely rejected on submit, so there's nothing invalid to pick.
    excluded_ids = {panel.id} | {p.id for p in _panel_descendants(panel)}
    other_panels = ElectricalPanel.query.join(Room).order_by(Room.name, ElectricalPanel.name).all()
    return render_template(
        'edit_window.html', item_label='ел. табло', saved=request.args.get('saved') == '1',
        action=url_for('admin_update_panel', panel_id=panel.id),
        fields=[
            {'name': 'name', 'label': 'Име на таблото', 'value': panel.name, 'type': 'text', 'required': True},
            {'name': 'room_id', 'label': 'Помещение', 'value': panel.room_id, 'type': 'select', 'options': [
                {'value': r.id, 'label': f'{r.building.name} / {r.name}'} for r in rooms
            ]},
            {'name': 'parent_panel_id', 'label': 'Захранва се от табло', 'value': panel.parent_panel_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- директно от мрежата --'}
            ] + [{'value': p.id, 'label': f'{p.room.name} / {p.name}'} for p in other_panels if p.id not in excluded_ids]},
            {'name': 'notes', 'label': 'Бележки', 'value': panel.notes or '', 'type': 'textarea'},
        ]
    )


@app.route('/admin/panels/<int:panel_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel(panel_id):
    panel = ElectricalPanel.query.get_or_404(panel_id)
    name = request.form.get('name', '').strip()
    room_id_raw = request.form.get('room_id', '')
    if not name or not room_id_raw.isdigit() or not db.session.get(Room, int(room_id_raw)):
        flash('Моля въведете име и валидно помещение.', 'danger')
        return redirect(url_for('admin_panels'))
    parent_id_raw = request.form.get('parent_panel_id', '')
    parent_id = None
    if parent_id_raw.isdigit():
        parent_id = int(parent_id_raw)
        excluded_ids = {panel.id} | {p.id for p in _panel_descendants(panel)}
        if parent_id in excluded_ids or not db.session.get(ElectricalPanel, parent_id):
            flash('Невалидно захранващо табло (води до кръгова връзка).', 'danger')
            return redirect(url_for('admin_panels'))
    panel.name = name
    panel.room_id = int(room_id_raw)
    panel.parent_panel_id = parent_id
    panel.notes = request.form.get('notes', '').strip() or None
    db.session.commit()
    flash(f'Ел. табло "{panel.name}" беше обновено.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_panel_window', panel_id=panel_id, saved='1'))
    return redirect(url_for('admin_panels'))


@app.route('/admin/panels/<int:panel_id>/room', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_room(panel_id):
    """Changes just an ElectricalPanel's room - leaves name/parent_panel_id/
    notes untouched, unlike posting to admin_update_panel() with only a
    room_id (which would blank those out). Used by the "Инвертори" section
    on /admin/power, so an inverter's physical room can be corrected right
    there without opening the full /admin/panels edit popup."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    room_id_raw = request.form.get('room_id', '')
    room = db.session.get(Room, int(room_id_raw)) if room_id_raw.isdigit() else None
    if not room:
        flash('Невалидно помещение.', 'danger')
        return redirect(url_for('admin_power'))
    panel.room_id = room.id
    db.session.commit()
    log_action(f'Преместено табло "{panel.name}" в помещение "{room.name}"')
    flash(f'Табло "{panel.name}" беше преместено в "{room.building.name} / {room.name}".', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/panels/<int:panel_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_panel(panel_id):
    """Unlinks (not cascade-deletes) any Machine/ShellyDevice/ModbusDevice/
    child-panel pointing at this panel - same reasoning as admin_delete_room().
    A child panel just becomes root-fed (parent_panel_id = None), same as it
    never having had a parent - it's still a perfectly real panel on its own."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    name = panel.name
    for machine in Machine.query.filter_by(panel_id=panel.id).all():
        machine.panel_id = None
    for device in ShellyDevice.query.filter_by(panel_id=panel.id).all():
        device.panel_id = None
    for device in ModbusDevice.query.filter_by(panel_id=panel.id).all():
        device.panel_id = None
    for child in ElectricalPanel.query.filter_by(parent_panel_id=panel.id).all():
        child.parent_panel_id = None
    db.session.delete(panel)
    db.session.commit()
    log_action(f'Изтрито ел. табло "{name}"')
    flash(f'Ел. табло "{name}" беше изтрито.', 'success')
    return redirect(url_for('admin_panels'))


# ----------------- СХЕМА НА ЕЛ. ТАБЛО (ВЪТРЕШНОСТ) -----------------
# One-line schematic INSIDE a single ElectricalPanel - breakers/fuses/
# contactors/etc. (PanelComponent), connected by drawn wires (PanelWire).
# A totally separate layer from the factory map (which positions whole
# panels relative to each other, never their internals) - see
# PanelComponent's docstring.

def _parse_panel_component_form(form):
    """Shared by admin_add_panel_component()/admin_update_panel_component() -
    returns (values_dict, None) or (None, error_message). 'feeds_target' is
    a single "kind:id" value (e.g. "machine:12") built by
    _panel_component_fields()'s combined dropdown - at most one of
    feeds_machine_id/feeds_panel_id/feeds_modbus_device_id ends up set."""
    name = form.get('name', '').strip()
    if not name:
        return None, 'Моля въведете име на елемента.'
    component_type = form.get('component_type', 'breaker')
    if component_type not in PANEL_COMPONENT_TYPES:
        component_type = 'breaker'
    rated_raw = form.get('rated_current_a', '').strip()
    poles_raw = form.get('poles', '').strip()
    try:
        rated_current_a = float(rated_raw) if rated_raw else None
    except ValueError:
        return None, 'Номиналният ток трябва да е число.'
    try:
        poles = int(poles_raw) if poles_raw else None
    except ValueError:
        return None, 'Броят полюси трябва да е цяло число.'
    feeds_machine_id = feeds_panel_id = feeds_modbus_device_id = None
    kind, _, raw_id = form.get('feeds_target', '').partition(':')
    if raw_id.isdigit():
        target_id = int(raw_id)
        if kind == 'machine' and db.session.get(Machine, target_id):
            feeds_machine_id = target_id
        elif kind == 'panel' and db.session.get(ElectricalPanel, target_id):
            feeds_panel_id = target_id
        elif kind == 'device' and db.session.get(ModbusDevice, target_id):
            feeds_modbus_device_id = target_id
    return {
        'component_type': component_type, 'name': name, 'rated_current_a': rated_current_a, 'poles': poles,
        'manufacturer': form.get('manufacturer', '').strip() or None, 'model': form.get('model', '').strip() or None,
        'notes': form.get('notes', '').strip() or None,
        'feeds_machine_id': feeds_machine_id, 'feeds_panel_id': feeds_panel_id, 'feeds_modbus_device_id': feeds_modbus_device_id,
    }, None


@app.route('/admin/panels/<int:panel_id>/schematic')
@role_required(['admin', 'worker'])
def admin_panel_schematic(panel_id):
    panel = ElectricalPanel.query.get_or_404(panel_id)
    components = PanelComponent.query.filter_by(panel_id=panel.id).order_by(PanelComponent.id).all()
    # A wire "belongs" to whichever panel it was drawn FROM (from_component is
    # always local to that panel - see admin_add_panel_wire()), but must also
    # show up here when the OTHER end points at one of THIS panel's own
    # components (a cross-panel link drawn from the other side) - explicit
    # ask: "да мога да връзвам... към обекти в други табла".
    wires = (PanelWire.query
             .join(PanelComponent, PanelWire.to_component_id == PanelComponent.id)
             .filter(db.or_(PanelWire.panel_id == panel.id, PanelComponent.panel_id == panel.id))
             .all())
    other_panels = ElectricalPanel.query.filter(ElectricalPanel.id != panel.id).order_by(ElectricalPanel.name).all()
    other_panels_data = {
        p.id: {
            'name': p.name,
            # parent_panel_id lets the auto one-line diagram (renderOneLineDiagram()
            # in admin_panel_schematic.html) resolve which end of an ambiguous
            # cross-panel wire (e.g. in-to-in, landing two panels' own incoming
            # main breakers on the same feeder cable) is actually upstream,
            # using the real distribution hierarchy instead of guessing from
            # wire side alone.
            'parent_panel_id': p.parent_panel_id,
            'components': [
                {'id': c.id, 'name': c.name, 'poles': c.poles or 1, 'type': c.component_type}
                for c in sorted(p.components, key=lambda c: c.name)
            ],
        }
        for p in other_panels
    }
    # Per-element hover tooltip data ("за всеки елемент да има тоолтип с
    # параметрите му и описание на връзките") - the connections themselves
    # aren't included here since they're already fully described client-side
    # via WIRES (including cross-panel ones) - see buildComponentTooltip()
    # in admin_panel_schematic.html.
    components_meta = {
        c.id: {
            'name': c.name, 'type': PANEL_COMPONENT_TYPES.get(c.component_type, c.component_type),
            'rated_current_a': c.rated_current_a, 'poles': c.poles,
            'manufacturer': c.manufacturer, 'model': c.model, 'notes': c.notes,
            'feeds_label': c.feeds_label,
        }
        for c in components
    }
    return render_template(
        'admin_panel_schematic.html', panel=panel, components=components, wires=wires,
        feeds_options=_feeds_target_options(panel), component_types=PANEL_COMPONENT_TYPES,
        phase_types=PANEL_WIRE_PHASE_TYPES, other_panels=other_panels, other_panels_data=other_panels_data,
        components_meta=components_meta, active_page='admin_panels',
    )


@app.route('/admin/panels/<int:panel_id>/schematic/background', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_upload_panel_background(panel_id):
    """Uploads (or replaces) the reference photo behind this panel's
    schematic - see ElectricalPanel.schematic_bg_filename's docstring. A
    fresh upload resets scale/position back to defaults (1x, centered)
    since a different photo's framing has nothing to do with the old one's."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    new_filename = _save_upload(request.files.get('image'), app.config['PANEL_BACKGROUND_FOLDER'], IMAGE_EXTENSIONS)
    if not new_filename:
        flash('Моля изберете валиден снимков файл (png/jpg/webp/gif).', 'danger')
        return redirect(url_for('admin_panel_schematic', panel_id=panel_id))
    if panel.schematic_bg_filename and _UPLOADED_IMAGE_PREFIX_RE.match(panel.schematic_bg_filename):
        old_path = os.path.join(app.config['PANEL_BACKGROUND_FOLDER'], panel.schematic_bg_filename)
        if os.path.exists(old_path):
            os.remove(old_path)
    panel.schematic_bg_filename = new_filename
    panel.schematic_bg_scale = 1.0
    panel.schematic_bg_pos_x = 50.0
    panel.schematic_bg_pos_y = 50.0
    panel.schematic_bg_opacity = 0.85
    db.session.commit()
    flash('Снимката за подложка беше качена.', 'success')
    return redirect(url_for('admin_panel_schematic', panel_id=panel_id))


@app.route('/admin/panels/<int:panel_id>/schematic/background/transform', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_background_transform(panel_id):
    """AJAX - saves the background photo's dragged position and/or its
    scale (a plain number input, not a drag handle - see
    admin_panel_schematic.html)."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    try:
        if 'pos_x' in request.form:
            panel.schematic_bg_pos_x = max(0.0, min(100.0, float(request.form['pos_x'])))
        if 'pos_y' in request.form:
            panel.schematic_bg_pos_y = max(0.0, min(100.0, float(request.form['pos_y'])))
        if 'scale' in request.form:
            panel.schematic_bg_scale = max(0.05, min(10.0, float(request.form['scale'])))
        if 'opacity' in request.form:
            panel.schematic_bg_opacity = max(0.0, min(1.0, float(request.form['opacity'])))
    except ValueError:
        return jsonify({'error': 'Невалидна стойност.'}), 400
    db.session.commit()
    return jsonify({
        'pos_x': panel.schematic_bg_pos_x, 'pos_y': panel.schematic_bg_pos_y, 'scale': panel.schematic_bg_scale,
        'opacity': panel.schematic_bg_opacity,
    })


@app.route('/admin/panels/<int:panel_id>/schematic/background/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_panel_background(panel_id):
    panel = ElectricalPanel.query.get_or_404(panel_id)
    if panel.schematic_bg_filename and _UPLOADED_IMAGE_PREFIX_RE.match(panel.schematic_bg_filename):
        old_path = os.path.join(app.config['PANEL_BACKGROUND_FOLDER'], panel.schematic_bg_filename)
        if os.path.exists(old_path):
            os.remove(old_path)
    panel.schematic_bg_filename = None
    db.session.commit()
    flash('Снимката за подложка беше премахната.', 'success')
    return redirect(url_for('admin_panel_schematic', panel_id=panel_id))


@app.route('/admin/panels/<int:panel_id>/schematic/components/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_panel_component(panel_id):
    panel = ElectricalPanel.query.get_or_404(panel_id)
    values, error = _parse_panel_component_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_panel_schematic', panel_id=panel_id))
    db.session.add(PanelComponent(panel_id=panel.id, **values))
    db.session.commit()
    log_action(f'Добавен елемент "{values["name"]}" в таблото "{panel.name}"')
    flash(f'Елемент "{values["name"]}" беше добавен.', 'success')
    return redirect(url_for('admin_panel_schematic', panel_id=panel_id))


def _feeds_target_options(panel):
    """Combined dropdown options for PanelComponent.feeds_target ("kind:id",
    see _parse_panel_component_form()) - every Machine/other-ElectricalPanel/
    ModbusDevice a component could point at as its downstream side. Shared
    by admin_panel_schematic()'s add-form and _panel_component_fields()'s
    edit popup so both offer the exact same choices."""
    machines = Machine.query.order_by(Machine.name).all()
    other_panels = ElectricalPanel.query.filter(ElectricalPanel.id != panel.id).join(Room).order_by(Room.name, ElectricalPanel.name).all()
    modbus_devices = ModbusDevice.query.order_by(ModbusDevice.name).all()
    options = [{'value': '', 'label': '-- нищо (описателно) --'}]
    options += [{'value': f'machine:{m.id}', 'label': f'Машина: {m.name}'} for m in machines]
    options += [{'value': f'panel:{p.id}', 'label': f'Табло: {p.name}'} for p in other_panels]
    options += [{'value': f'device:{d.id}', 'label': f'Устройство: {d.name}'} for d in modbus_devices]
    return options


def _panel_component_fields(panel, component=None):
    """Field list for edit_window.html - shared shape by
    edit_panel_component_window(). 'feeds_target' combines all three
    possible targets into one dropdown (see _parse_panel_component_form()'s
    docstring)."""
    def v(attr, default=''):
        value = getattr(component, attr) if component is not None else None
        return value if value is not None else default
    current_feeds = ''
    if component is not None:
        if component.feeds_machine_id:
            current_feeds = f'machine:{component.feeds_machine_id}'
        elif component.feeds_panel_id:
            current_feeds = f'panel:{component.feeds_panel_id}'
        elif component.feeds_modbus_device_id:
            current_feeds = f'device:{component.feeds_modbus_device_id}'
    return [
        {'name': 'name', 'label': 'Име', 'value': v('name'), 'type': 'text', 'required': True},
        {'name': 'component_type', 'label': 'Тип', 'value': v('component_type', 'breaker'), 'type': 'select',
         'options': [{'value': k, 'label': lbl} for k, lbl in PANEL_COMPONENT_TYPES.items()]},
        {'name': 'rated_current_a', 'label': 'Номинален ток (A)', 'value': v('rated_current_a'), 'type': 'text'},
        {'name': 'poles', 'label': 'Брой полюси', 'value': v('poles'), 'type': 'text'},
        {'name': 'manufacturer', 'label': 'Производител (по избор)', 'value': v('manufacturer'), 'type': 'text'},
        {'name': 'model', 'label': 'Модел (по избор)', 'value': v('model'), 'type': 'text'},
        {'name': 'feeds_target', 'label': 'Захранва', 'value': current_feeds, 'type': 'select', 'options': _feeds_target_options(panel)},
        {'name': 'notes', 'label': 'Бележки', 'value': v('notes'), 'type': 'textarea'},
    ]


@app.route('/admin/panel-components/<int:component_id>/edit')
@role_required(['admin', 'worker'])
def edit_panel_component_window(component_id):
    component = PanelComponent.query.get_or_404(component_id)
    return render_template(
        'edit_window.html', item_label=component.name, saved=request.args.get('saved') == '1',
        action=url_for('admin_update_panel_component', component_id=component.id),
        fields=_panel_component_fields(component.panel, component),
    )


@app.route('/admin/panel-components/<int:component_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_component(component_id):
    component = PanelComponent.query.get_or_404(component_id)
    values, error = _parse_panel_component_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('edit_panel_component_window', component_id=component_id))
    for key, value in values.items():
        setattr(component, key, value)
    db.session.commit()
    flash(f'Елемент "{component.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_panel_component_window', component_id=component_id, saved='1'))
    return redirect(url_for('admin_panel_schematic', panel_id=component.panel_id))


@app.route('/admin/panel-components/<int:component_id>/position', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_component_position(component_id):
    """Saves a component's dragged (x, y) - see admin_update_machine_position()."""
    component = PanelComponent.query.get_or_404(component_id)
    try:
        pos_x = float(request.form.get('pos_x', ''))
        pos_y = float(request.form.get('pos_y', ''))
    except ValueError:
        return jsonify({'error': 'Невалидна позиция.'}), 400
    component.pos_x = max(0.0, min(100.0, pos_x))
    component.pos_y = max(0.0, min(100.0, pos_y))
    db.session.commit()
    return jsonify({'pos_x': component.pos_x, 'pos_y': component.pos_y})


@app.route('/admin/panel-components/<int:component_id>/scale', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_component_scale(component_id):
    """AJAX - the +/- resize buttons on each card in
    admin_panel_schematic.html, independent of every other component's own
    scale (see PanelComponent.scale's docstring)."""
    component = PanelComponent.query.get_or_404(component_id)
    try:
        scale = float(request.form.get('scale', ''))
    except ValueError:
        return jsonify({'error': 'Невалиден мащаб.'}), 400
    component.scale = max(0.4, min(3.0, scale))
    db.session.commit()
    return jsonify({'scale': component.scale})


@app.route('/admin/panel-components/<int:component_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_panel_component(component_id):
    component = PanelComponent.query.get_or_404(component_id)
    panel_id = component.panel_id
    name = component.name
    # Wires aren't ORM-cascade-configured (from/to_component_id are plain
    # NOT NULL FKs) - deleting a component that still has one raises
    # IntegrityError instead of silently nulling it out, so drop them first.
    PanelWire.query.filter_by(from_component_id=component_id).delete(synchronize_session=False)
    PanelWire.query.filter_by(to_component_id=component_id).delete(synchronize_session=False)
    db.session.delete(component)
    db.session.commit()
    log_action(f'Изтрит елемент "{name}" от таблото')
    flash(f'Елемент "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_panel_schematic', panel_id=panel_id))


@app.route('/admin/panels/<int:panel_id>/schematic/wires/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_panel_wire(panel_id):
    """AJAX (not a form redirect) - see admin_panel_schematic.html's
    "click one terminal then another" connect mode, which draws the new
    wire immediately from the response rather than reloading the page.
    Endpoints are a specific numbered pole + side ('in'/'out'/'tap' - see
    PanelWire's docstring), not just "this component" - the same two
    components can have several wires between them (one per pole)."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    try:
        from_id = int(request.form.get('from_component_id', ''))
        to_id = int(request.form.get('to_component_id', ''))
        from_pole = int(request.form.get('from_pole', '1'))
        to_pole = int(request.form.get('to_pole', '1'))
    except ValueError:
        return jsonify({'error': 'Невалидни елементи.'}), 400
    from_side = request.form.get('from_side', 'out')
    to_side = request.form.get('to_side', 'in')
    if from_side not in ('in', 'out', 'tap') or to_side not in ('in', 'out', 'tap'):
        return jsonify({'error': 'Невалидна страна на полюса.'}), 400
    if from_pole < 1 or to_pole < 1:
        return jsonify({'error': 'Невалиден полюс.'}), 400
    if from_id == to_id and from_pole == to_pole and from_side == to_side:
        return jsonify({'error': 'Не може да свържете полюс със самия себе си.'}), 400
    # from_component is always the terminal clicked on THIS panel's own
    # canvas; to_component may belong to a different panel entirely - a real
    # cable leaving this cabinet toward another one ("да мога да връзвам
    # устройства... към обекти в други табла"), shown there as a labeled
    # stub back to here (see admin_panel_schematic.html's redrawWires()).
    from_component = PanelComponent.query.filter_by(id=from_id, panel_id=panel.id).first()
    to_component = PanelComponent.query.get(to_id)
    if not from_component or not to_component:
        return jsonify({'error': 'Елементът не принадлежи на това табло.'}), 400
    phase_type = request.form.get('phase_type', 'three_phase')
    if phase_type not in PANEL_WIRE_PHASE_TYPES:
        phase_type = 'three_phase'
    if phase_type == 'three_phase' and ((from_component.poles or 0) < 3 or (to_component.poles or 0) < 3):
        return jsonify({'error': 'Трифазна връзка изисква и двата елемента да имат поне 3 полюса.'}), 400
    type_error = _panel_wire_type_error(phase_type, from_component, to_component)
    if type_error:
        return jsonify({'error': type_error}), 400
    def _same_terminal(w, comp_id, pole, side):
        return w.from_component_id == comp_id and w.from_pole == pole and w.from_side == side or \
            w.to_component_id == comp_id and w.to_pole == pole and w.to_side == side
    # Checked globally, not scoped to this panel - a duplicate could already
    # exist as a wire "owned" by the OTHER panel if to_component lives there.
    candidates = PanelWire.query.filter(db.or_(
        PanelWire.from_component_id.in_([from_id, to_id]), PanelWire.to_component_id.in_([from_id, to_id])
    )).all()
    existing = [w for w in candidates
                if _same_terminal(w, from_id, from_pole, from_side) and _same_terminal(w, to_id, to_pole, to_side)]
    if existing:
        return jsonify({'error': 'Вече има връзка между тези полюси.'}), 400
    wire = PanelWire(
        panel_id=panel.id, from_component_id=from_id, to_component_id=to_id,
        from_pole=from_pole, from_side=from_side, to_pole=to_pole, to_side=to_side, phase_type=phase_type,
    )
    db.session.add(wire)
    db.session.commit()
    return jsonify({
        'id': wire.id, 'from_component_id': from_id, 'to_component_id': to_id,
        'from_pole': from_pole, 'from_side': from_side, 'to_pole': to_pole, 'to_side': to_side,
        'phase_type': phase_type,
    })


@app.route('/admin/panel-wires/<int:wire_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_panel_wire(wire_id):
    """AJAX, same reasoning as admin_add_panel_wire(). A three-phase wire is
    always one of 3 rows (one per pole) making up a single physical
    connection - deleting any one of them deletes the whole bundle, not
    just that pole ("когато триеш трифазна връзка, една от линиите изтрива
    и трите")."""
    wire = PanelWire.query.get_or_404(wire_id)
    if wire.phase_type == 'three_phase':
        PanelWire.query.filter_by(
            from_component_id=wire.from_component_id, from_side=wire.from_side,
            to_component_id=wire.to_component_id, to_side=wire.to_side, phase_type='three_phase',
        ).delete(synchronize_session=False)
    else:
        db.session.delete(wire)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/panel-wires/<int:wire_id>/retarget', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_retarget_panel_wire(wire_id):
    """Changes ONE end (from or to) of an existing wire to a different
    component/side, in place - lets a connection be redirected without
    deleting and redrawing it from scratch (explicit ask: "редактиране на
    връзките... смяна на целта"). For a three-phase wire this applies to
    the WHOLE bundle (all 3 pole rows) at once, each keeping its own pole
    number matched against the new target - "когато редактираш трифазно,
    промяната на един от трите [реда] да променя и трите". AJAX, same
    reasoning/validation as admin_add_panel_wire(); the OTHER end of each
    row is left untouched."""
    wire = PanelWire.query.get_or_404(wire_id)
    end = request.form.get('end')
    if end not in ('from', 'to'):
        return jsonify({'error': 'Невалиден край на връзката.'}), 400
    try:
        new_id = int(request.form.get('component_id', ''))
    except ValueError:
        return jsonify({'error': 'Невалиден елемент.'}), 400
    new_side = request.form.get('side', '')
    if new_side not in ('in', 'out', 'tap'):
        return jsonify({'error': 'Невалидна страна на полюса.'}), 400
    new_component = PanelComponent.query.get(new_id)
    if not new_component:
        return jsonify({'error': 'Елементът не съществува.'}), 400
    type_error = _panel_wire_type_error(wire.phase_type, new_component)
    if type_error:
        return jsonify({'error': type_error}), 400

    if wire.phase_type == 'three_phase':
        if (new_component.poles or 0) < 3:
            return jsonify({'error': 'Трифазна връзка изисква елемент с поне 3 полюса.'}), 400
        bundle = PanelWire.query.filter_by(
            from_component_id=wire.from_component_id, from_side=wire.from_side,
            to_component_id=wire.to_component_id, to_side=wire.to_side, phase_type='three_phase',
        ).all()
    else:
        try:
            form_pole = int(request.form.get('pole', '1'))
        except ValueError:
            return jsonify({'error': 'Невалиден полюс.'}), 400
        if form_pole < 1:
            return jsonify({'error': 'Невалиден полюс.'}), 400
        bundle = [wire]

    def _same_terminal(w, comp_id, pole, side):
        return w.from_component_id == comp_id and w.from_pole == pole and w.from_side == side or \
            w.to_component_id == comp_id and w.to_pole == pole and w.to_side == side

    bundle_ids = [w.id for w in bundle]
    updates = []
    for w in bundle:
        pole = (w.from_pole if end == 'from' else w.to_pole) if wire.phase_type == 'three_phase' else form_pole
        other_id = w.to_component_id if end == 'from' else w.from_component_id
        other_pole = w.to_pole if end == 'from' else w.from_pole
        other_side = w.to_side if end == 'from' else w.from_side
        if new_id == other_id and pole == other_pole and new_side == other_side:
            return jsonify({'error': 'Не може да свържете полюс със самия себе си.'}), 400
        candidates = PanelWire.query.filter(
            PanelWire.id.notin_(bundle_ids),
            db.or_(PanelWire.from_component_id.in_([new_id, other_id]), PanelWire.to_component_id.in_([new_id, other_id])),
        ).all()
        if any(_same_terminal(c, new_id, pole, new_side) and _same_terminal(c, other_id, other_pole, other_side) for c in candidates):
            return jsonify({'error': 'Вече има връзка между тези полюси.'}), 400
        updates.append((w, pole))

    for w, pole in updates:
        if end == 'from':
            w.from_component_id, w.from_pole, w.from_side = new_id, pole, new_side
        else:
            w.to_component_id, w.to_pole, w.to_side = new_id, pole, new_side
    db.session.commit()
    return jsonify({'ok': True})


# ----------------- БАТЕРИЙНО СТОПАНСТВО (ШКАФ / STACK / БАТЕРИЯ) -----------------
# Шкаф (Cabinet) holds one or more BatteryStack ("STACK" - a group of
# series-connected modules behind one BMS port of one Solis inverter, see
# _solis_snapshot()'s 'battery_groups'), each stack holding individual
# Battery rows. Purely a catalogue/inventory - none of this is read live off
# the inverter; battery_count/place_label on BatteryStack are the only
# derived bits.

@app.route('/admin/battery-cabinets')
@role_required(['admin', 'worker'])
def admin_battery_cabinets():
    cabinets = Cabinet.query.order_by(Cabinet.name).all()
    solis_devices = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.name).all()
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    return render_template(
        'admin_battery_cabinets.html', cabinets=cabinets, solis_devices=solis_devices, rooms=rooms,
        bms_ports=BATTERY_STACK_BMS_PORTS, source_types=BATTERY_STACK_SOURCE_TYPES,
        active_page='admin_battery_cabinets'
    )


@app.route('/admin/battery-cabinets/data')
@role_required(['admin', 'worker'])
def admin_battery_cabinets_data():
    """JSON feed polled by admin_battery_cabinets.html for each stack's live
    SOC/voltage/temperature - see _battery_stack_snapshots()."""
    stacks = BatteryStack.query.all()
    live = _battery_stack_snapshots(stacks)
    return jsonify({'ts': datetime.now().strftime('%H:%M:%S'), 'stacks': {str(k): v for k, v in live.items()}})


@app.route('/admin/battery-cabinets/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_cabinet():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на шкафа.', 'danger')
        return redirect(url_for('admin_battery_cabinets'))
    db.session.add(Cabinet(name=name, notes=request.form.get('notes', '').strip() or None))
    db.session.commit()
    log_action(f'Добавен шкаф "{name}"')
    flash(f'Шкаф "{name}" беше добавен.', 'success')
    return redirect(url_for('admin_battery_cabinets'))


@app.route('/admin/battery-cabinets/<int:cabinet_id>/edit')
@role_required(['admin', 'worker'])
def edit_cabinet_window(cabinet_id):
    cabinet = Cabinet.query.get_or_404(cabinet_id)
    return render_template(
        'edit_window.html', item_label='шкаф', saved=request.args.get('saved') == '1',
        action=url_for('admin_update_cabinet', cabinet_id=cabinet.id),
        fields=[
            {'name': 'name', 'label': 'Име на шкафа', 'value': cabinet.name, 'type': 'text', 'required': True},
            {'name': 'notes', 'label': 'Бележки', 'value': cabinet.notes or '', 'type': 'textarea'},
        ]
    )


@app.route('/admin/battery-cabinets/<int:cabinet_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_cabinet(cabinet_id):
    cabinet = Cabinet.query.get_or_404(cabinet_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на шкафа.', 'danger')
        return redirect(url_for('admin_battery_cabinets'))
    cabinet.name = name
    cabinet.notes = request.form.get('notes', '').strip() or None
    db.session.commit()
    flash(f'Шкаф "{cabinet.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_cabinet_window', cabinet_id=cabinet_id, saved='1'))
    return redirect(url_for('admin_battery_cabinets'))


@app.route('/admin/battery-cabinets/<int:cabinet_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_cabinet(cabinet_id):
    cabinet = Cabinet.query.get_or_404(cabinet_id)
    name = cabinet.name
    db.session.delete(cabinet)  # cascades to its stacks, which cascade to their batteries
    db.session.commit()
    log_action(f'Изтрит шкаф "{name}"')
    flash(f'Шкаф "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_battery_cabinets'))


def _parse_stack_form(form):
    """Shared by admin_add_stack()/admin_update_stack() - returns a dict of
    column values, or (None, error_message) if something required is missing/
    invalid."""
    name = form.get('name', '').strip()
    if not name:
        return None, 'Моля въведете име на stack-а.'
    source_type = form.get('source_type', 'inverter')
    if source_type not in BATTERY_STACK_SOURCE_TYPES:
        source_type = 'inverter'
    inverter_id_raw = form.get('inverter_device_id', '')
    inverter_id = int(inverter_id_raw) if inverter_id_raw.isdigit() and db.session.get(ModbusDevice, int(inverter_id_raw)) else None
    bms_port = form.get('bms_port', '')
    if bms_port not in BATTERY_STACK_BMS_PORTS:
        bms_port = None
    room_id_raw = form.get('room_id', '')
    room_id = int(room_id_raw) if room_id_raw.isdigit() and db.session.get(Room, int(room_id_raw)) else None
    min_raw = form.get('min_batteries', '').strip()
    max_raw = form.get('max_batteries', '').strip()
    return {
        'name': name,
        'source_type': source_type,
        'inverter_device_id': inverter_id,
        'bms_port': bms_port,
        'brand': form.get('brand', '').strip() or None,
        'model': form.get('model', '').strip() or None,
        'serial_number': form.get('serial_number', '').strip() or None,
        'min_batteries': int(min_raw) if min_raw.isdigit() else None,
        'max_batteries': int(max_raw) if max_raw.isdigit() else None,
        'room_id': room_id,
    }, None


@app.route('/admin/battery-stacks/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_stack():
    cabinet_id_raw = request.form.get('cabinet_id', '')
    cabinet = db.session.get(Cabinet, int(cabinet_id_raw)) if cabinet_id_raw.isdigit() else None
    if not cabinet:
        flash('Невалиден шкаф.', 'danger')
        return redirect(url_for('admin_battery_cabinets'))
    values, error = _parse_stack_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_battery_cabinets'))
    db.session.add(BatteryStack(cabinet_id=cabinet.id, **values))
    db.session.commit()
    log_action(f'Добавен stack "{values["name"]}" в шкаф "{cabinet.name}"')
    flash(f'Stack "{values["name"]}" беше добавен.', 'success')
    return redirect(url_for('admin_battery_cabinets'))


@app.route('/admin/battery-stacks/<int:stack_id>/edit')
@role_required(['admin', 'worker'])
def edit_stack_window(stack_id):
    stack = BatteryStack.query.get_or_404(stack_id)
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    solis_devices = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.name).all()
    return render_template(
        'edit_window.html', item_label='stack', saved=request.args.get('saved') == '1',
        action=url_for('admin_update_stack', stack_id=stack.id),
        fields=[
            {'name': 'name', 'label': 'Име на stack-а', 'value': stack.name, 'type': 'text', 'required': True},
            {'name': 'source_type', 'label': 'Източник на живи данни', 'value': stack.source_type, 'type': 'select', 'options': [
                {'value': k, 'label': v} for k, v in BATTERY_STACK_SOURCE_TYPES.items()
            ]},
            {'name': 'inverter_device_id', 'label': 'Инвертор', 'value': stack.inverter_device_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': d.id, 'label': d.name} for d in solis_devices]},
            {'name': 'bms_port', 'label': 'БМС порт', 'value': stack.bms_port or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': k, 'label': v} for k, v in BATTERY_STACK_BMS_PORTS.items()]},
            {'name': 'brand', 'label': 'Марка (по избор)', 'value': stack.brand or '', 'type': 'text'},
            {'name': 'model', 'label': 'Модел (по избор)', 'value': stack.model or '', 'type': 'text'},
            {'name': 'serial_number', 'label': 'Сериен номер (по избор)', 'value': stack.serial_number or '', 'type': 'text'},
            {'name': 'min_batteries', 'label': 'Минимален брой батерии', 'value': stack.min_batteries or '', 'type': 'text'},
            {'name': 'max_batteries', 'label': 'Максимален брой батерии', 'value': stack.max_batteries or '', 'type': 'text'},
            {'name': 'room_id', 'label': 'Помещение', 'value': stack.room_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': r.id, 'label': f'{r.building.name} / {r.name}'} for r in rooms]},
        ]
    )


@app.route('/admin/battery-stacks/<int:stack_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_stack(stack_id):
    stack = BatteryStack.query.get_or_404(stack_id)
    values, error = _parse_stack_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_battery_cabinets'))
    for key, value in values.items():
        setattr(stack, key, value)
    db.session.commit()
    flash(f'Stack "{stack.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_stack_window', stack_id=stack_id, saved='1'))
    return redirect(url_for('admin_battery_cabinets'))


@app.route('/admin/battery-stacks/<int:stack_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_stack(stack_id):
    stack = BatteryStack.query.get_or_404(stack_id)
    name = stack.name
    db.session.delete(stack)  # cascades to its batteries
    db.session.commit()
    log_action(f'Изтрит stack "{name}"')
    flash(f'Stack "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_battery_cabinets'))


@app.route('/admin/battery-stacks/<int:stack_id>/position', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_stack_position(stack_id):
    """Saves a stack's dragged (x, y) on its room's map - see admin_update_machine_position()."""
    stack = BatteryStack.query.get_or_404(stack_id)
    try:
        pos_x = float(request.form.get('pos_x', ''))
        pos_y = float(request.form.get('pos_y', ''))
    except ValueError:
        return jsonify({'error': 'Невалидна позиция.'}), 400
    stack.pos_x = max(0.0, min(100.0, pos_x))
    stack.pos_y = max(0.0, min(100.0, pos_y))
    db.session.commit()
    return jsonify({'pos_x': stack.pos_x, 'pos_y': stack.pos_y})


@app.route('/admin/battery-stacks/<int:stack_id>/batteries')
@role_required(['admin', 'worker'])
def admin_stack_batteries_window(stack_id):
    """Small popup (own template, not edit_window.html - a repeating list +
    add-row form, not a flat field list) for managing the individual
    Battery rows inside one stack."""
    stack = BatteryStack.query.get_or_404(stack_id)
    battery_models = BatteryModel.query.order_by(BatteryModel.name).all()
    return render_template('admin_stack_batteries.html', stack=stack, battery_models=battery_models)


def _parse_battery_form(form):
    """Shared by admin_add_battery()/admin_update_battery() - returns
    (model_id, voltage, capacity, serial_number, error). When a model is
    picked, its nominal voltage/capacity are authoritative (the form's
    fields are auto-filled by JS from the same model, but this re-derives
    server-side rather than trust whatever the client sent for those two)."""
    model_id_raw = form.get('model_id', '').strip()
    voltage_raw = form.get('voltage', '').strip()
    capacity_raw = form.get('capacity_ah', '').strip()
    try:
        model_id = int(model_id_raw) if model_id_raw else None
        voltage = float(voltage_raw) if voltage_raw else None
        capacity = float(capacity_raw) if capacity_raw else None
    except ValueError:
        return None, None, None, None, 'Напрежението и капацитетът трябва да са числа.'
    battery_model = BatteryModel.query.get(model_id) if model_id else None
    if battery_model:
        voltage = battery_model.nominal_voltage_v
        capacity = battery_model.capacity_ah
    serial_number = form.get('serial_number', '').strip() or None
    return (battery_model.id if battery_model else None), voltage, capacity, serial_number, None


@app.route('/admin/battery-stacks/<int:stack_id>/batteries/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_battery(stack_id):
    stack = BatteryStack.query.get_or_404(stack_id)
    model_id, voltage, capacity, serial_number, error = _parse_battery_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_stack_batteries_window', stack_id=stack_id))
    db.session.add(Battery(
        stack_id=stack.id, model_id=model_id, voltage=voltage, capacity_ah=capacity,
        serial_number=serial_number,
    ))
    db.session.commit()
    log_action(f'Добавена батерия в stack "{stack.name}"')
    flash('Батерията беше добавена.', 'success')
    return redirect(url_for('admin_stack_batteries_window', stack_id=stack_id))


@app.route('/admin/batteries/<int:battery_id>/edit')
@role_required(['admin', 'worker'])
def edit_battery_window(battery_id):
    """Own template (not edit_window.html) so it can reuse the exact same
    model-dropdown/energy-display JS as the add form on
    admin_stack_batteries.html - stays in the same small popup window
    (no window.open nesting), navigating back to the battery list on save
    or cancel."""
    battery = Battery.query.get_or_404(battery_id)
    battery_models = BatteryModel.query.order_by(BatteryModel.name).all()
    return render_template('edit_battery_window.html', battery=battery, battery_models=battery_models)


@app.route('/admin/batteries/<int:battery_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_battery(battery_id):
    battery = Battery.query.get_or_404(battery_id)
    model_id, voltage, capacity, serial_number, error = _parse_battery_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('edit_battery_window', battery_id=battery_id))
    battery.model_id = model_id
    battery.voltage = voltage
    battery.capacity_ah = capacity
    battery.serial_number = serial_number
    db.session.commit()
    log_action(f'Обновена батерия #{battery_id}')
    flash('Батерията беше обновена.', 'success')
    return redirect(url_for('admin_stack_batteries_window', stack_id=battery.stack_id))


@app.route('/admin/batteries/<int:battery_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_battery(battery_id):
    battery = Battery.query.get_or_404(battery_id)
    stack_id = battery.stack_id
    db.session.delete(battery)
    db.session.commit()
    log_action(f'Изтрита батерия #{battery_id}')
    flash('Батерията беше изтрита.', 'success')
    return redirect(url_for('admin_stack_batteries_window', stack_id=stack_id))


BATTERY_MODEL_FLOAT_FIELDS = [
    'nominal_voltage_v', 'capacity_ah', 'energy_kwh', 'usable_energy_kwh',
    'continuous_current_a', 'max_discharge_power_kw', 'round_trip_efficiency_pct',
    'length_mm', 'width_mm', 'height_mm', 'weight_kg',
]
BATTERY_MODEL_TEXT_FIELDS = ['manufacturer', 'chemistry', 'protection_rating', 'communication', 'notes']


def _parse_battery_model_form(form):
    """Shared by admin_add_battery_model()/admin_update_battery_model() -
    returns a dict of column values, or (None, error_message) if the name or
    one of the two required numeric fields is missing/invalid."""
    name = form.get('name', '').strip()
    if not name:
        return None, 'Моля въведете име на модела.'
    values = {'name': name}
    for field in BATTERY_MODEL_FLOAT_FIELDS:
        raw = form.get(field, '').strip()
        if not raw:
            values[field] = None
            continue
        try:
            values[field] = float(raw)
        except ValueError:
            return None, f'Полето "{field}" трябва да е число.'
    if values['nominal_voltage_v'] is None or values['capacity_ah'] is None:
        return None, 'Напрежението и капацитетът са задължителни.'
    cycle_life_raw = form.get('cycle_life', '').strip()
    if cycle_life_raw:
        try:
            values['cycle_life'] = int(cycle_life_raw)
        except ValueError:
            return None, 'Броят цикли трябва да е цяло число.'
    else:
        values['cycle_life'] = None
    for field in BATTERY_MODEL_TEXT_FIELDS:
        values[field] = form.get(field, '').strip() or None
    if not values['manufacturer']:
        values['manufacturer'] = 'Dyness'
    return values, None


@app.route('/admin/battery-models')
@role_required(['admin', 'worker'])
def admin_battery_models():
    models = BatteryModel.query.order_by(BatteryModel.name).all()
    return render_template('admin_battery_models.html', models=models, active_page='admin_battery_cabinets')


def _battery_model_fields(model=None):
    def v(attr, default=''):
        return getattr(model, attr) if model is not None and getattr(model, attr) is not None else default
    return [
        {'name': 'name', 'label': 'Име на модела', 'value': v('name'), 'type': 'text', 'required': True},
        {'name': 'manufacturer', 'label': 'Производител', 'value': v('manufacturer', 'Dyness'), 'type': 'text'},
        {'name': 'chemistry', 'label': 'Химия', 'value': v('chemistry'), 'type': 'text'},
        {'name': 'nominal_voltage_v', 'label': 'Номинално напрежение (V)', 'value': v('nominal_voltage_v'), 'type': 'text', 'required': True},
        {'name': 'capacity_ah', 'label': 'Капацитет (Ah)', 'value': v('capacity_ah'), 'type': 'text', 'required': True},
        {'name': 'energy_kwh', 'label': 'Енергийно съдържание (kWh)', 'value': v('energy_kwh'), 'type': 'text'},
        {'name': 'usable_energy_kwh', 'label': 'Използваема енергия (kWh)', 'value': v('usable_energy_kwh'), 'type': 'text'},
        {'name': 'continuous_current_a', 'label': 'Продължителен ток (A)', 'value': v('continuous_current_a'), 'type': 'text'},
        {'name': 'max_discharge_power_kw', 'label': 'Макс. мощност на разряд (kW)', 'value': v('max_discharge_power_kw'), 'type': 'text'},
        {'name': 'round_trip_efficiency_pct', 'label': 'Ефективност (%)', 'value': v('round_trip_efficiency_pct'), 'type': 'text'},
        {'name': 'cycle_life', 'label': 'Брой цикли', 'value': v('cycle_life'), 'type': 'text'},
        {'name': 'length_mm', 'label': 'Дължина (мм)', 'value': v('length_mm'), 'type': 'text'},
        {'name': 'width_mm', 'label': 'Широчина (мм)', 'value': v('width_mm'), 'type': 'text'},
        {'name': 'height_mm', 'label': 'Височина (мм)', 'value': v('height_mm'), 'type': 'text'},
        {'name': 'weight_kg', 'label': 'Тегло (кг)', 'value': v('weight_kg'), 'type': 'text'},
        {'name': 'protection_rating', 'label': 'Клас на защита', 'value': v('protection_rating'), 'type': 'text'},
        {'name': 'communication', 'label': 'Комуникация', 'value': v('communication'), 'type': 'text'},
        {'name': 'notes', 'label': 'Бележки', 'value': v('notes'), 'type': 'textarea'},
    ]


@app.route('/admin/battery-models/new')
@role_required(['admin', 'worker'])
def new_battery_model_window():
    return render_template(
        'edit_window.html', item_label='нов модел батерия', saved=request.args.get('saved') == '1',
        action=url_for('admin_add_battery_model'), fields=_battery_model_fields()
    )


@app.route('/admin/battery-models/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_battery_model():
    values, error = _parse_battery_model_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_battery_models'))
    if BatteryModel.query.filter_by(name=values['name']).first():
        flash(f'Вече има модел с име "{values["name"]}".', 'danger')
        return redirect(url_for('admin_battery_models'))
    model = BatteryModel(**values)
    db.session.add(model)
    db.session.commit()
    log_action(f'Добавен модел батерия "{model.name}"')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_battery_model_window', model_id=model.id, saved='1'))
    flash(f'Моделът "{model.name}" беше добавен.', 'success')
    return redirect(url_for('admin_battery_models'))


@app.route('/admin/battery-models/<int:model_id>/edit')
@role_required(['admin', 'worker'])
def edit_battery_model_window(model_id):
    model = BatteryModel.query.get_or_404(model_id)
    return render_template(
        'edit_window.html', item_label=model.name, saved=request.args.get('saved') == '1',
        action=url_for('admin_update_battery_model', model_id=model.id), fields=_battery_model_fields(model)
    )


@app.route('/admin/battery-models/<int:model_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_battery_model(model_id):
    model = BatteryModel.query.get_or_404(model_id)
    values, error = _parse_battery_model_form(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin_battery_models'))
    existing = BatteryModel.query.filter_by(name=values['name']).first()
    if existing and existing.id != model.id:
        flash(f'Вече има модел с име "{values["name"]}".', 'danger')
        return redirect(url_for('admin_battery_models'))
    for key, value in values.items():
        setattr(model, key, value)
    db.session.commit()
    flash(f'Моделът "{model.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_battery_model_window', model_id=model_id, saved='1'))
    return redirect(url_for('admin_battery_models'))


@app.route('/admin/battery-models/<int:model_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_battery_model(model_id):
    model = BatteryModel.query.get_or_404(model_id)
    in_use = Battery.query.filter_by(model_id=model.id).count()
    if in_use:
        flash(f'Моделът "{model.name}" се използва от {in_use} батерии и не може да бъде изтрит.', 'danger')
        return redirect(url_for('admin_battery_models'))
    name = model.name
    db.session.delete(model)
    db.session.commit()
    log_action(f'Изтрит модел батерия "{name}"')
    flash(f'Моделът "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_battery_models'))


# ----------------- ТЕМПЕРАТУРНИ СЕНЗОРИ -----------------

@app.route('/admin/temperature-sensors')
@role_required(['admin', 'worker'])
def admin_temperature_sensors():
    sensors = TemperatureSensor.query.order_by(TemperatureSensor.name).all()
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    return render_template(
        'admin_temperature_sensors.html', sensors=sensors, rooms=rooms,
        sensor_types=TEMP_SENSOR_TYPES, active_page='admin_temperature_sensors'
    )


@app.route('/admin/temperature-sensors/data')
@role_required(['admin', 'worker'])
def admin_temperature_sensors_data():
    """JSON feed polled by admin_temperature_sensors.html - live temperature/
    humidity/battery per sensor, keyed by id."""
    sensors = TemperatureSensor.query.all()
    return jsonify({
        'ts': datetime.now().strftime('%H:%M:%S'),
        'sensors': {s.id: _mqtt_temp_snapshot(s) for s in sensors},
    })


@app.route('/admin/temperature-sensors/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_temperature_sensor():
    name = request.form.get('name', '').strip()
    mqtt_topic = request.form.get('mqtt_topic', '').strip().strip('/')
    if not name or not mqtt_topic:
        flash('Моля въведете име и MQTT тема на сензора.', 'danger')
        return redirect(url_for('admin_temperature_sensors'))
    if TemperatureSensor.query.filter_by(mqtt_topic=mqtt_topic).first():
        flash(f'Вече има сензор с MQTT тема "{mqtt_topic}".', 'danger')
        return redirect(url_for('admin_temperature_sensors'))
    room_id_raw = request.form.get('room_id', '')
    room_id = int(room_id_raw) if room_id_raw.isdigit() and db.session.get(Room, int(room_id_raw)) else None
    location_label = request.form.get('location_label', '').strip() or None
    sensor_type = request.form.get('sensor_type', '')
    if sensor_type not in TEMP_SENSOR_TYPES:
        sensor_type = 'shelly_ht_gen1'
    db.session.add(TemperatureSensor(
        name=name, mqtt_topic=mqtt_topic, sensor_type=sensor_type, room_id=room_id,
        location_label=None if room_id else location_label,
    ))
    db.session.commit()
    log_action(f'Добавен температурен сензор "{name}"')
    flash(f'Сензор "{name}" беше добавен.', 'success')
    return redirect(url_for('admin_temperature_sensors'))


@app.route('/admin/temperature-sensors/<int:sensor_id>/edit')
@role_required(['admin', 'worker'])
def edit_temperature_sensor_window(sensor_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/temperature-sensors."""
    sensor = TemperatureSensor.query.get_or_404(sensor_id)
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    return render_template(
        'edit_window.html', item_label='температурен сензор', saved=request.args.get('saved') == '1',
        action=url_for('admin_update_temperature_sensor', sensor_id=sensor.id),
        fields=[
            {'name': 'name', 'label': 'Име на сензора', 'value': sensor.name, 'type': 'text', 'required': True},
            {'name': 'mqtt_topic', 'label': 'MQTT тема', 'value': sensor.mqtt_topic, 'type': 'text', 'required': True},
            {'name': 'sensor_type', 'label': 'Вид сензор', 'value': sensor.sensor_type, 'type': 'select',
             'options': [{'value': k, 'label': v} for k, v in TEMP_SENSOR_TYPES.items()]},
            {'name': 'room_id', 'label': 'Помещение', 'value': sensor.room_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': r.id, 'label': f'{r.building.name} / {r.name}'} for r in rooms]},
            {'name': 'location_label', 'label': 'Или свободно място (ако не е в помещение)',
             'value': sensor.location_label or '', 'type': 'text'},
        ]
    )


@app.route('/admin/temperature-sensors/<int:sensor_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_temperature_sensor(sensor_id):
    sensor = TemperatureSensor.query.get_or_404(sensor_id)
    name = request.form.get('name', '').strip()
    mqtt_topic = request.form.get('mqtt_topic', '').strip().strip('/')
    if not name or not mqtt_topic:
        flash('Моля въведете име и MQTT тема на сензора.', 'danger')
        return redirect(url_for('admin_temperature_sensors'))
    if TemperatureSensor.query.filter(TemperatureSensor.mqtt_topic == mqtt_topic, TemperatureSensor.id != sensor.id).first():
        flash(f'Вече има друг сензор с MQTT тема "{mqtt_topic}".', 'danger')
        return redirect(url_for('admin_temperature_sensors'))
    room_id_raw = request.form.get('room_id', '')
    room_id = int(room_id_raw) if room_id_raw.isdigit() and db.session.get(Room, int(room_id_raw)) else None
    sensor_type = request.form.get('sensor_type', '')
    if sensor_type not in TEMP_SENSOR_TYPES:
        sensor_type = sensor.sensor_type
    sensor.name = name
    sensor.mqtt_topic = mqtt_topic
    sensor.sensor_type = sensor_type
    sensor.room_id = room_id
    sensor.location_label = None if room_id else (request.form.get('location_label', '').strip() or None)
    db.session.commit()
    flash(f'Сензор "{sensor.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_temperature_sensor_window', sensor_id=sensor_id, saved='1'))
    return redirect(url_for('admin_temperature_sensors'))


@app.route('/admin/temperature-sensors/<int:sensor_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_temperature_sensor(sensor_id):
    sensor = TemperatureSensor.query.get_or_404(sensor_id)
    name = sensor.name
    db.session.delete(sensor)
    db.session.commit()
    log_action(f'Изтрит температурен сензор "{name}"')
    flash(f'Сензор "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_temperature_sensors'))


# ----------------- КОНВЕКТОРИ -----------------

@app.route('/admin/convectors')
@role_required(['admin', 'worker'])
def admin_convectors():
    convectors = Convector.query.order_by(Convector.name).all()
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    return render_template(
        'admin_convectors.html', convectors=convectors, rooms=rooms,
        convector_types=CONVECTOR_TYPES, connection_types=CONVECTOR_CONNECTION_TYPES,
        active_page='admin_convectors'
    )


@app.route('/admin/convectors/data')
@role_required(['admin', 'worker'])
def admin_convectors_data():
    """JSON feed polled by admin_convectors.html - live on/off state per convector."""
    convectors = Convector.query.all()
    return jsonify({
        'ts': datetime.now().strftime('%H:%M:%S'),
        'convectors': {c.id: _shelly_convector_status(c) for c in convectors},
    })


@app.route('/admin/convectors/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_convector():
    name = request.form.get('name', '').strip()
    host = request.form.get('host', '').strip()
    mqtt_topic = request.form.get('mqtt_topic', '').strip().strip('/')
    connection_type = request.form.get('connection_type', 'ip')
    if connection_type not in CONVECTOR_CONNECTION_TYPES:
        connection_type = 'ip'
    if not name:
        flash('Моля въведете име на конвектора.', 'danger')
        return redirect(url_for('admin_convectors'))
    if connection_type == 'ip' and not host:
        flash('Моля въведете адрес (IP) за връзка тип "IP (HTTP)".', 'danger')
        return redirect(url_for('admin_convectors'))
    if connection_type == 'mqtt' and not mqtt_topic:
        flash('Моля въведете MQTT тема за връзка тип "MQTT".', 'danger')
        return redirect(url_for('admin_convectors'))
    if mqtt_topic and Convector.query.filter_by(mqtt_topic=mqtt_topic).first():
        flash(f'Вече има конвектор с MQTT тема "{mqtt_topic}".', 'danger')
        return redirect(url_for('admin_convectors'))
    device_type = request.form.get('device_type', '')
    if device_type not in CONVECTOR_TYPES:
        device_type = 'shelly_gen1'
    channel_raw = request.form.get('relay_channel', '0')
    relay_channel = int(channel_raw) if channel_raw.isdigit() else 0
    room_id_raw = request.form.get('room_id', '')
    room_id = int(room_id_raw) if room_id_raw.isdigit() and db.session.get(Room, int(room_id_raw)) else None
    location_label = request.form.get('location_label', '').strip() or None
    db.session.add(Convector(
        name=name, connection_type=connection_type, host=host or None, mqtt_topic=mqtt_topic or None,
        device_type=device_type, relay_channel=relay_channel, room_id=room_id,
        location_label=None if room_id else location_label,
    ))
    db.session.commit()
    log_action(f'Добавен конвектор "{name}"')
    flash(f'Конвектор "{name}" беше добавен.', 'success')
    return redirect(url_for('admin_convectors'))


@app.route('/admin/convectors/<int:conv_id>/edit')
@role_required(['admin', 'worker'])
def edit_convector_window(conv_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/convectors."""
    conv = Convector.query.get_or_404(conv_id)
    rooms = Room.query.join(Building).order_by(Building.name, Room.name).all()
    return render_template(
        'edit_window.html', item_label='конвектор', saved=request.args.get('saved') == '1',
        action=url_for('admin_update_convector', conv_id=conv.id),
        fields=[
            {'name': 'name', 'label': 'Име на конвектора', 'value': conv.name, 'type': 'text', 'required': True},
            {'name': 'connection_type', 'label': 'Вид връзка', 'value': conv.connection_type, 'type': 'select',
             'options': [{'value': k, 'label': v} for k, v in CONVECTOR_CONNECTION_TYPES.items()]},
            {'name': 'host', 'label': 'Адрес (IP, за връзка тип IP)', 'value': conv.host or '', 'type': 'text'},
            {'name': 'mqtt_topic', 'label': 'MQTT тема (за връзка тип MQTT)', 'value': conv.mqtt_topic or '', 'type': 'text'},
            {'name': 'device_type', 'label': 'Вид устройство', 'value': conv.device_type, 'type': 'select',
             'options': [{'value': k, 'label': v} for k, v in CONVECTOR_TYPES.items()]},
            {'name': 'relay_channel', 'label': 'Номер на реле (0 за еднoканални)', 'value': conv.relay_channel, 'type': 'text'},
            {'name': 'room_id', 'label': 'Помещение', 'value': conv.room_id or '', 'type': 'select', 'options': [
                {'value': '', 'label': '-- няма --'}
            ] + [{'value': r.id, 'label': f'{r.building.name} / {r.name}'} for r in rooms]},
            {'name': 'location_label', 'label': 'Или свободно място (ако не е в помещение)',
             'value': conv.location_label or '', 'type': 'text'},
        ]
    )


@app.route('/admin/convectors/<int:conv_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_convector(conv_id):
    conv = Convector.query.get_or_404(conv_id)
    name = request.form.get('name', '').strip()
    host = request.form.get('host', '').strip()
    mqtt_topic = request.form.get('mqtt_topic', '').strip().strip('/')
    connection_type = request.form.get('connection_type', '')
    if connection_type not in CONVECTOR_CONNECTION_TYPES:
        connection_type = conv.connection_type
    if not name:
        flash('Моля въведете име на конвектора.', 'danger')
        return redirect(url_for('admin_convectors'))
    if connection_type == 'ip' and not host:
        flash('Моля въведете адрес (IP) за връзка тип "IP (HTTP)".', 'danger')
        return redirect(url_for('admin_convectors'))
    if connection_type == 'mqtt' and not mqtt_topic:
        flash('Моля въведете MQTT тема за връзка тип "MQTT".', 'danger')
        return redirect(url_for('admin_convectors'))
    if mqtt_topic and Convector.query.filter(Convector.mqtt_topic == mqtt_topic, Convector.id != conv.id).first():
        flash(f'Вече има друг конвектор с MQTT тема "{mqtt_topic}".', 'danger')
        return redirect(url_for('admin_convectors'))
    device_type = request.form.get('device_type', '')
    if device_type not in CONVECTOR_TYPES:
        device_type = conv.device_type
    channel_raw = request.form.get('relay_channel', '0')
    relay_channel = int(channel_raw) if channel_raw.isdigit() else conv.relay_channel
    room_id_raw = request.form.get('room_id', '')
    room_id = int(room_id_raw) if room_id_raw.isdigit() and db.session.get(Room, int(room_id_raw)) else None
    conv.name = name
    conv.connection_type = connection_type
    conv.host = host or None
    conv.mqtt_topic = mqtt_topic or None
    conv.device_type = device_type
    conv.relay_channel = relay_channel
    conv.room_id = room_id
    conv.location_label = None if room_id else (request.form.get('location_label', '').strip() or None)
    db.session.commit()
    flash(f'Конвектор "{conv.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_convector_window', conv_id=conv_id, saved='1'))
    return redirect(url_for('admin_convectors'))


@app.route('/admin/convectors/<int:conv_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_convector(conv_id):
    conv = Convector.query.get_or_404(conv_id)
    name = conv.name
    db.session.delete(conv)
    db.session.commit()
    log_action(f'Изтрит конвектор "{name}"')
    flash(f'Конвектор "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_convectors'))


@app.route('/admin/convectors/<int:conv_id>/toggle', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_convector_toggle(conv_id):
    """The one control action in this app that switches real hardware - see
    Convector's docstring/_shelly_convector_set() for why this device class
    (unlike Machines/meters) is allowed to. Reads current state first so
    "toggle" always flips from what the device actually reports, not from
    possibly-stale state in this request."""
    conv = Convector.query.get_or_404(conv_id)
    status = _shelly_convector_status(conv)
    if not status['online']:
        flash(f'Конвектор "{conv.name}" не отговаря - не може да се превключи.', 'danger')
        return redirect(url_for('admin_convectors'))
    turn_on = not status['is_on']
    try:
        _shelly_convector_set(conv, turn_on)
    except Exception:
        flash(f'Неуспешно превключване на "{conv.name}" - устройството не отговори на командата.', 'danger')
        return redirect(url_for('admin_convectors'))
    log_action(f'{"Включен" if turn_on else "Изключен"} конвектор "{conv.name}"')
    flash(f'Конвектор "{conv.name}" беше {"включен" if turn_on else "изключен"}.', 'success')
    return redirect(url_for('admin_convectors'))


# ----------------- АВТОМОБИЛНО СТОПАНСТВО -----------------
# Company vehicle fleet - tracks 3 recurring legal deadlines per vehicle
# (insurance/ГО, vignette, technical inspection). See Vehicle/
# vehicle_deadline_status()/inject_vehicle_alerts() above for how the
# warning-then-daily-reminder behaviour works (recomputed per request, no
# cron/email for v1).

def _parse_form_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


@app.route('/admin/vehicles')
@role_required(['admin', 'worker'])
def admin_vehicles():
    vehicles = Vehicle.query.order_by(Vehicle.name).all()
    return render_template('admin_vehicles.html', vehicles=vehicles, active_page='admin_vehicles')


@app.route('/admin/vehicles/create', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_add_vehicle():
    name = request.form.get('name', '').strip()
    license_plate = request.form.get('license_plate', '').strip().upper()
    if not name:
        flash('Моля въведете име на автомобила.', 'danger')
        return redirect(url_for('admin_vehicles'))
    if not license_plate:
        flash('Моля въведете регистрационен номер.', 'danger')
        return redirect(url_for('admin_vehicles'))
    if Vehicle.query.filter_by(license_plate=license_plate).first():
        flash(f'Вече има автомобил с номер "{license_plate}".', 'danger')
        return redirect(url_for('admin_vehicles'))
    db.session.add(Vehicle(
        name=name, license_plate=license_plate,
        brand=request.form.get('brand', '').strip() or None,
        model=request.form.get('model', '').strip() or None,
        vin=request.form.get('vin', '').strip() or None,
        responsible_name=request.form.get('responsible_name', '').strip() or None,
        insurance_expiry=_parse_form_date(request.form.get('insurance_expiry')),
        insurance_installments=request.form.get('insurance_installments') == '1',
        vignette_expiry=_parse_form_date(request.form.get('vignette_expiry')),
        inspection_expiry=_parse_form_date(request.form.get('inspection_expiry')),
        notes=request.form.get('notes', '').strip() or None,
    ))
    db.session.commit()
    log_action(f'Добавен автомобил "{name}" ({license_plate})')
    flash(f'Автомобил "{name}" беше добавен.', 'success')
    return redirect(url_for('admin_vehicles'))


@app.route('/admin/vehicles/<int:vehicle_id>/edit')
@role_required(['admin', 'worker'])
def edit_vehicle_window(vehicle_id):
    """Popup edit window (see edit_window.html) - opened via the pencil icon on /admin/vehicles."""
    v = Vehicle.query.get_or_404(vehicle_id)
    return render_template(
        'edit_window.html', item_label='автомобил', saved=request.args.get('saved') == '1',
        action=url_for('admin_update_vehicle', vehicle_id=v.id),
        fields=[
            {'name': 'name', 'label': 'Име', 'value': v.name, 'type': 'text', 'required': True},
            {'name': 'license_plate', 'label': 'Регистрационен номер', 'value': v.license_plate, 'type': 'text', 'required': True},
            {'name': 'brand', 'label': 'Марка', 'value': v.brand or '', 'type': 'text'},
            {'name': 'model', 'label': 'Модел', 'value': v.model or '', 'type': 'text'},
            {'name': 'vin', 'label': 'Рама (VIN)', 'value': v.vin or '', 'type': 'text'},
            {'name': 'responsible_name', 'label': 'Отговорник', 'value': v.responsible_name or '', 'type': 'text'},
            {'name': 'insurance_expiry', 'label': 'Застраховка (ГО) - валидна до', 'value': v.insurance_expiry or '', 'type': 'date'},
            {'name': 'insurance_installments', 'label': 'Изплаща се на вноски (тримесечно)', 'value': v.insurance_installments, 'type': 'checkbox'},
            {'name': 'vignette_expiry', 'label': 'Винетка - валидна до', 'value': v.vignette_expiry or '', 'type': 'date'},
            {'name': 'inspection_expiry', 'label': 'Технически преглед - валиден до', 'value': v.inspection_expiry or '', 'type': 'date'},
            {'name': 'notes', 'label': 'Забележка', 'value': v.notes or '', 'type': 'textarea'},
        ]
    )


@app.route('/admin/vehicles/<int:vehicle_id>/update', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_vehicle(vehicle_id):
    v = Vehicle.query.get_or_404(vehicle_id)
    name = request.form.get('name', '').strip()
    license_plate = request.form.get('license_plate', '').strip().upper()
    if not name:
        flash('Моля въведете име на автомобила.', 'danger')
        return redirect(url_for('admin_vehicles'))
    if not license_plate:
        flash('Моля въведете регистрационен номер.', 'danger')
        return redirect(url_for('admin_vehicles'))
    if Vehicle.query.filter(Vehicle.license_plate == license_plate, Vehicle.id != v.id).first():
        flash(f'Вече има друг автомобил с номер "{license_plate}".', 'danger')
        return redirect(url_for('admin_vehicles'))
    v.name = name
    v.license_plate = license_plate
    v.brand = request.form.get('brand', '').strip() or None
    v.model = request.form.get('model', '').strip() or None
    v.vin = request.form.get('vin', '').strip() or None
    v.responsible_name = request.form.get('responsible_name', '').strip() or None
    v.insurance_expiry = _parse_form_date(request.form.get('insurance_expiry'))
    v.insurance_installments = request.form.get('insurance_installments') == '1'
    v.vignette_expiry = _parse_form_date(request.form.get('vignette_expiry'))
    v.inspection_expiry = _parse_form_date(request.form.get('inspection_expiry'))
    v.notes = request.form.get('notes', '').strip() or None
    db.session.commit()
    flash(f'Автомобил "{v.name}" беше обновен.', 'success')
    if request.form.get('popup') == '1':
        return redirect(url_for('edit_vehicle_window', vehicle_id=vehicle_id, saved='1'))
    return redirect(url_for('admin_vehicles'))


@app.route('/admin/vehicles/<int:vehicle_id>/delete', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_delete_vehicle(vehicle_id):
    v = Vehicle.query.get_or_404(vehicle_id)
    name, plate = v.name, v.license_plate
    db.session.delete(v)
    db.session.commit()
    log_action(f'Изтрит автомобил "{name}" ({plate})')
    flash(f'Автомобил "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_vehicles'))


@app.route('/admin/vehicles/<int:vehicle_id>/installments/toggle', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_vehicle_installment_toggle(vehicle_id):
    """Marks one auto-calculated quarterly ГО due date paid/unpaid - the
    due_date form field must be one of Vehicle.insurance_installment_dates
    for this vehicle (silently ignored otherwise, e.g. a stale checkbox
    from before insurance_expiry was changed)."""
    v = Vehicle.query.get_or_404(vehicle_id)
    due_date = _parse_form_date(request.form.get('due_date'))
    if not due_date or due_date not in v.insurance_installment_dates:
        return redirect(url_for('admin_vehicles'))
    record = VehicleInsuranceInstallment.query.filter_by(vehicle_id=v.id, due_date=due_date).first()
    if not record:
        record = VehicleInsuranceInstallment(vehicle_id=v.id, due_date=due_date, paid=False)
        db.session.add(record)
    record.paid = not record.paid
    record.paid_at = datetime.utcnow() if record.paid else None
    db.session.commit()
    log_action(f'{"Отбелязана платена" if record.paid else "Отбелязана неплатена"} вноска ГО ({due_date.strftime("%d.%m.%Y")}) за "{v.name}"')
    return redirect(url_for('admin_vehicles'))


# ----------------- MODBUS ЕЛЕКТРОМЕРИ -----------------
# Read-only register access for Modbus TCP meters (e.g. DTSU666) - a generic
# raw-register tool (admin_modbus_read_registers()) plus a confirmed decoded
# view for DTSU666 specifically (_dtsu666_snapshot(), DTSU666_REGISTERS).
# MODBUS_READ_TIMEOUT mirrors SHELLY_TIMEOUT's reasoning: a slow/dead meter
# must fail fast, not hang the request.

MODBUS_READ_TIMEOUT = 3.0
# Cheap WiFi/RS-485-to-TCP gateways (like the one in front of this shop's
# DTSU666) often can't handle more than one Modbus TCP connection at a time -
# a second request arriving mid-transaction gets a garbled/short response
# instead of a clean error (this is what _dtsu666_snapshot()'s length check
# is defending against). One lock per (host, port) serializes every read
# against that gateway, regardless of which route/poll triggered it.
_modbus_locks = {}
_modbus_locks_guard = threading.Lock()


def _modbus_lock_for(host, port):
    key = (host, port)
    with _modbus_locks_guard:
        return _modbus_locks.setdefault(key, threading.Lock())


def _modbus_read_raw(host, port, unit_id, address, count, input_type):
    """
    Reads `count` 16-bit registers starting at `address` (0-based, matching
    the offset most meter manuals print - a manual's "40001" style 1-based
    address is that minus one). Returns (registers, error) - error is a
    human string, never raises, same tolerance as shelly_device_snapshot()
    towards a meter that's unreachable or rejects the request (wrong
    unit_id, wrong function code for that address range, etc.). Serialized
    per (host, port) - see _modbus_lock_for()'s docstring.
    """
    lock = _modbus_lock_for(host, port)
    with lock:
        client = ModbusTcpClient(host, port=port, timeout=MODBUS_READ_TIMEOUT)
        try:
            if not client.connect():
                return None, 'Няма връзка с устройството.'
            if input_type == 'input':
                result = client.read_input_registers(address, count=count, device_id=unit_id)
            else:
                result = client.read_holding_registers(address, count=count, device_id=unit_id)
            if result.isError():
                return None, str(result)
            return result.registers, None
        except Exception as e:
            return None, f'{type(e).__name__}: {e}'
        finally:
            client.close()


def _modbus_decode(registers, data_type):
    """
    Reinterprets a flat uint16 list as the requested type, 2 registers per
    32-bit value, high register first (the most common word order for these
    meters, per DTSU666-adjacent documentation - swap_words in the UI covers
    the other one without a code change). Returns a list of decoded values
    (one per `count`, or one per pair for 32-bit types).
    """
    import struct
    if data_type == 'uint16':
        return list(registers)
    if data_type == 'int16':
        return [r - 0x10000 if r >= 0x8000 else r for r in registers]
    pairs = list(zip(registers[0::2], registers[1::2]))
    values = []
    for hi, lo in pairs:
        raw = struct.pack('>HH', hi, lo)
        if data_type == 'uint32':
            values.append(struct.unpack('>I', raw)[0])
        elif data_type == 'int32':
            values.append(struct.unpack('>i', raw)[0])
        elif data_type == 'float32':
            values.append(round(struct.unpack('>f', raw)[0], 4))
    return values


# DTSU666 register map, confirmed empirically against a real unit (Unit ID
# 12 @ 192.168.18.119:26 - "DTSU-666-Главно") via admin_modbus_read_registers():
# every voltage/current/power value below was read live and cross-checked
# for internal consistency (S^2 = P^2 + Q^2 per phase matched to within
# rounding on all three phases), not taken from a datasheet. All holding
# registers (function 0x03), float32 (2 registers, big-endian words+bytes).
# Energy registers (0x4000 block) match the expected scale (hundreds of kWh
# on a running shop meter) and the standard total/forward/reverse layout,
# but weren't cross-checked as rigorously as the power block - flagged below.
DTSU666_REGISTERS = {
    'voltage_ll': (0x2000, 3),   # Uab, Ubc, Uca - line-to-line volts
    'voltage_ln': (0x2006, 3),   # Ua, Ub, Uc - phase-to-neutral volts
    'current': (0x200C, 3),      # Ia, Ib, Ic - amps
    'active_power': (0x2012, 4),   # total, A, B, C - watts
    'reactive_power': (0x201A, 4),  # total, A, B, C - var
    'apparent_power': (0x2022, 4),  # total, A, B, C - VA
    # unverified against the meter's own display - see docstring above
    'energy_active_total': (0x4000, 1),   # Wh
    'energy_active_forward': (0x4002, 1),  # Wh (import)
    'energy_active_reverse': (0x4004, 1),  # Wh (export)
}


def _dtsu666_snapshot(device):
    """
    Render-ready dict for a DTSU666, same shape as shelly_device_snapshot()'s
    return value so it can eventually feed the same kind of dashboard/panel
    aggregation - see DTSU666_REGISTERS for how each field was confirmed.
    """
    def read_block(name):
        address, count = DTSU666_REGISTERS[name]
        registers, error = _modbus_read_raw(device.host, device.port, device.unit_id, address, count * 2, 'holding')
        if error:
            raise RuntimeError(error)
        values = _modbus_decode(registers, 'float32')
        if len(values) != count:
            # A gateway under concurrent load (e.g. this page's own live poll
            # racing a manual register read) can return a short/garbled
            # response without pymodbus flagging it as an error - treat that
            # the same as a real error rather than indexing into a
            # too-short list further down.
            raise RuntimeError(f'Непълен отговор за "{name}" ({len(values)}/{count} стойности).')
        return values

    try:
        u_ln = read_block('voltage_ln')
        i = read_block('current')
        p = read_block('active_power')
        q = read_block('reactive_power')
        s = read_block('apparent_power')
        energy_total = read_block('energy_active_total')[0]

        labels = ['Фаза A', 'Фаза B', 'Фаза C']
        channels = [{
            'label': labels[idx], 'voltage': u_ln[idx], 'current': i[idx],
            'act_power': p[idx + 1], 'aprt_power': s[idx + 1], 'pf': None, 'freq': None,
        } for idx in range(3)]
    except Exception as e:
        return {
            'name': device.name, 'host': f'{device.host}:{device.port}', 'online': False,
            'error': str(e), 'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
            'temperature': None, 'rssi': None,
        }

    return {
        'name': device.name, 'host': f'{device.host}:{device.port}', 'online': True, 'error': None,
        'channels': channels, 'total_power': round(p[0], 1), 'total_energy': round(energy_total / 1000.0, 2),
        'temperature': None, 'rssi': None,
    }


# Solis S6 hybrid inverter register map, confirmed against the official
# manufacturer protocol ("RS485_MODBUS(ESINV-33000ID) Hybrid Inverter",
# 2020.9.15 - publicly published by Solis/Ginlong) and then cross-checked
# live against this shop's two units through admin_modbus_read_registers():
# every field lined up with something independently verifiable, not just
# "looked reasonable" -
#   - today/this-month/this-year generation vs. the real elapsed days matched
#     (2 days into September read ~94kWh/day, August's full-month total
#     matched ~148kWh/day - the same install)
#   - sum of the 4 PV string powers matched the documented total DC power
#   - DC bus half-voltage register read exactly half of DC bus voltage
#   - AC active power, backup load power, and inverting/rectifying power
#     (three separately documented registers) all read the identical 1900W
#   - battery voltage (main register) and battery voltage "from BMS" (a
#     different register, different scale) matched to within 2V
# Same empirical-confirmation bar as DTSU666_REGISTERS above, actually
# stronger (multiple independent cross-checks, not just one). All input
# registers (function 0x04), read as two contiguous blocks (33089-33092 is
# reserved/unused but cheap to read along with its neighbors) over a single
# TCP connection - see _solis_read_blocks(). This shop's WiFi-to-Modbus
# stick takes seconds to accept each *new* connection (unlike the DTSU666's
# gateway, which is near-instant) but is fast once connected, so the
# request count barely matters - the connection count is what was making a
# live poll take 11-14s per inverter before this was one connection instead
# of three.
SOLIS_BLOCK_ENERGY_PV_AC = (33029, 66)    # 33029-33094: PV strings/energy/AC output/temp/freq
# 33118-33217: fault bits, meter, battery, loads, "fast" (<1s, unsmoothed)
# battery current - widened from the original 33126-33168 to also cover
# 33118 (Battery Fault Status Bits) and 33217 (Battery Current Fast), both
# confirmed against github.com/Pho3niX90/solis_modbus's independently
# reverse-engineered register map. That project's much larger register set
# (settings/TOU schedules/fault bits/etc.) still only ever documents ONE
# aggregate battery reading here - it does NOT document a per-port split.
SOLIS_BLOCK_METER_BATTERY = (33118, 100)
SOLIS_BLOCK_GENERATOR = (33530, 6)   # 33530-33535: generator port power/energy (see 'generator' below)
# 34328-34393: the "Smart Port" block that project documents (voltage/current
# at 34328-34333, power at 34391-34393 - both all-zero on this shop's units,
# since no generator is actually wired to the smart port) ALSO contains, in
# the UNDOCUMENTED registers in between, two identical-shaped 25-register
# groups (34346-34365 and 34371-34390) found by directly probing this shop's
# two inverters - each group ends in a constant 11 (matching "11 batteries
# per port") preceded by a constant 16 (matching Dyness Stack100's 16S/51.2V
# internal cell count), and otherwise carries a handful of values that vary
# between the two groups and between inverters in a battery-plausible way
# (a temperature-like pair, a cell-voltage-like pair) - see
# _solis_snapshot()'s 'battery_groups' (EXPERIMENTAL - field meaning is this
# file's own inference from the value patterns, not confirmed documentation
# or a vendor-provided register map, per explicit user sign-off).
SOLIS_BLOCK_SMARTPORT = (34328, 66)
# CONFIRMED (2026-09-06, live cross-check against SolisCloud while the two
# BMS ports genuinely differed: port 1 94%, port 2 98%): register 34278 is
# Battery 2's own real SOC, exact match. This also revealed that register
# 33139 - used everywhere in this file as "the" aggregate battery SOC - is
# actually NOT an aggregate at all: it read 94 at that exact moment, an
# exact match for BATTERY 1's own SOC. There is no true aggregate/average
# register; 33139 and 34278 are the two real, independent per-port SOC
# readings all along. See SOLIS_INVERTER_MODBUS.md for the full comparison.
#
# Read as its OWN separate small spec in _solis_read_extra_blocks rather
# than folded into SOLIS_BLOCK_SMARTPORT above by widening that block's
# range - widening IT from 66 to 119 registers to reach backwards to 34275
# briefly caused exactly that (real symptom: "зацикля когато идеш на
# страница която чете батериите" right after that change went in) - most
# likely this device's/gateway's own read-size ceiling sits somewhere
# between 100 and 119 registers (SOLIS_BLOCK_METER_BATTERY's own 100 reads
# fine; 119 evidently doesn't), even though the Modbus PDU limit generally
# allows up to ~125. A few extra registers in the SAME connection (see
# _solis_read_blocks() - one spec per address range, still one TCP
# connection) is zero-risk by comparison - this block is only 4 registers.
#
# 34275 (u16, /100) = Battery 2's own real voltage - CONFIRMED (2026-09-06,
# exact match: 587.4V live cross-check against SolisCloud). By the same
# logic as 33139/33133 turning out to be Battery 1's own SOC/voltage (not
# an aggregate), 34276 is Battery 2's own current (EXPERIMENTAL - only
# cross-checked at 0A/idle so far, which every nearby register also reads;
# needs re-confirming next time the two ports draw genuinely different,
# non-zero current - see SOLIS_INVERTER_MODBUS.md). 34277 is unidentified
# (reads ~26, temperature-plausible but not yet checked against anything).
SOLIS_BLOCK_BATTERY2_EXTRA = (34275, 4)  # 34275=voltage, 34276=current, 34277=?, 34278=soc (CONFIRMED)

# Full per-phase reading of the external grid CT meter (33251-33286,
# documented in github.com/Pho3niX90/solis_modbus as "Meter 1") - explicit
# ask: "Виртуалния смарт метър... е трифазен. Записвай всички данни от
# него за всички фази". A first live read looked wrong (current scaled as
# /10 implied tens of amps per phase and several kW, while both SolisCloud
# itself and this app's own already-trusted single-value meter reading
# (33126-33131, still used for the simple 'meter' dict) showed ~0kW) -
# CORRECTED per the user's own catch: current here is in MILLIAMPS (/1000),
# not deciamps. With that fix every field cross-checks internally (each
# phase's own apparent/reactive power sums EXACTLY to the block's own
# totals) and frequency reads a plausible 49.96-50.02Hz - all CONFIRMED
# 2026-09-06 by that consistency, not by an external ground truth (unlike
# the battery discoveries, SolisCloud's own UI doesn't expose a per-phase
# meter breakdown to cross-check against).
SOLIS_BLOCK_METER_3P = (33251, 36)


def _solis_u16(regs, base, addr):
    return regs[addr - base]


def _solis_s16(regs, base, addr):
    v = _solis_u16(regs, base, addr)
    return v - 0x10000 if v >= 0x8000 else v


def _solis_u32(regs, base, addr):
    hi, lo = regs[addr - base], regs[addr - base + 1]
    return (hi << 16) | lo


def _solis_s32(regs, base, addr):
    v = _solis_u32(regs, base, addr)
    return v - 0x100000000 if v >= 0x80000000 else v


def _solis_read_blocks(device, specs):
    """
    Reads every (start, count) in `specs` as input registers (function 0x04)
    over a single Modbus TCP connection, instead of _modbus_read_raw()'s one
    connection per call - see SOLIS_BLOCK_* above for why that matters here.
    Still serialized per (host, port) via _modbus_lock_for(), same as every
    other Modbus read, since this shop's two Solis inverters share one WiFi
    stick (same host:port, different unit_id) and the DTSU666 gateway
    pattern already established that these cheap gateways choke on
    concurrent connections. Returns a list of register lists, one per spec;
    raises RuntimeError on any failure (unreachable device, wrong unit_id,
    short/garbled response under concurrent load - same tolerance as
    _dtsu666_snapshot()'s read_block()).
    """
    lock = _modbus_lock_for(device.host, device.port)
    with lock:
        client = ModbusTcpClient(device.host, port=device.port, timeout=MODBUS_READ_TIMEOUT)
        try:
            if not client.connect():
                raise RuntimeError('Няма връзка с устройството.')
            blocks = []
            for start, count in specs:
                result = client.read_input_registers(start, count=count, device_id=device.unit_id)
                if result.isError():
                    raise RuntimeError(str(result))
                if len(result.registers) != count:
                    raise RuntimeError(f'Непълен отговор за {start} ({len(result.registers)}/{count} стойности).')
                blocks.append(result.registers)
            return blocks
        finally:
            client.close()


# Generator/smart-port + the experimental per-battery-port data (see
# SOLIS_BLOCK_GENERATOR/SOLIS_BLOCK_SMARTPORT) changes slowly - reading it
# on every _solis_snapshot() call briefly overloaded this shop's shared
# WiFi-to-Modbus gateway once it was added to the hot live-poll path
# (admin_power.html's refresh picker allows as often as once a second, times
# 2 inverters sharing one gateway/lock): pymodbus started reporting
# transaction-ID-mismatch errors and requests hung for 20+ seconds. Cached
# per-device and only actually re-read once every SOLIS_EXTRA_REFRESH_SECONDS,
# regardless of how often the live snapshot itself is polled.
SOLIS_EXTRA_REFRESH_SECONDS = 60
_solis_extra_cache = {}
_solis_extra_cache_lock = threading.Lock()


def _solis_read_extra_blocks(device):
    b4_start, b4_count = SOLIS_BLOCK_GENERATOR
    b5_start, b5_count = SOLIS_BLOCK_SMARTPORT
    b6_start, b6_count = SOLIS_BLOCK_BATTERY2_EXTRA
    b7_start, b7_count = SOLIS_BLOCK_METER_3P
    b4, b5, b6, b7 = _solis_read_blocks(
        device, [(b4_start, b4_count), (b5_start, b5_count), (b6_start, b6_count), (b7_start, b7_count)]
    )
    u16_4 = lambda addr: _solis_u16(b4, b4_start, addr)
    s16_4 = lambda addr: _solis_s16(b4, b4_start, addr)
    u32_4 = lambda addr: _solis_u32(b4, b4_start, addr)
    u16_5 = lambda addr: _solis_u16(b5, b5_start, addr)
    u16_6 = lambda addr: _solis_u16(b6, b6_start, addr)
    s16_6 = lambda addr: _solis_s16(b6, b6_start, addr)
    u16_7 = lambda addr: _solis_u16(b7, b7_start, addr)
    s16_7 = lambda addr: _solis_s16(b7, b7_start, addr)
    s32_7 = lambda addr: _solis_s32(b7, b7_start, addr)
    u32_7 = lambda addr: _solis_u32(b7, b7_start, addr)

    # EXPERIMENTAL - one entry per battery port, decoded from a pattern found
    # by directly probing this shop's two inverters (see
    # SOLIS_BLOCK_SMARTPORT's docstring), not from any documented register
    # map. Shown to the user labeled as such, not as confirmed data -
    # correct on the strength of "ends in 11, matches 11 batteries per port"
    # plus plausible voltage/temperature variation between the two groups,
    # nothing more. base_addr+15 was originally guessed as SOC, but a live
    # probe while the bank was actively discharging (aggregate SOC 90%/89%
    # per inverter - see 'battery'.'soc' below) showed it pinned at a flat
    # 100 on BOTH ports of BOTH inverters regardless - implausible for a
    # live SOC, but it exactly matches the aggregate SOH (also 100 on both
    # units), so it's relabeled 'soh' here instead ("не показва коректно
    # soc на батериите").
    #
    # The REAL per-port SOC turned out to live elsewhere entirely (34278,
    # not in this 25-register group at all) - see SOLIS_BLOCK_SMARTPORT's
    # docstring for the full story. _battery_stack_live_data() below wires
    # port 1's SOC from register 33139 and port 2's from 34278 - both
    # confirmed exact matches against SolisCloud, not from this dict.
    #
    # base_addr+0 (labeled 'voltage' here previously) was likewise cross-
    # checked against SolisCloud's own per-battery detail page (real user
    # comparison, 2026-09-06): it reads a constant 501.6 on BOTH ports of
    # this inverter, identical to SolisCloud's own "BMS Discharge Voltage
    # Limit Value" setting - NOT the live pack voltage (583-586V there,
    # varying and different per port) that the old key name implied.
    # Renamed to what it actually is; cell_voltage_min/max below are
    # confirmed correct against the same page (exact/near-exact match) and
    # remain the real live per-port voltage indicator.
    def _decode_battery_group(base_addr):
        return {
            'discharge_voltage_limit': round(u16_5(base_addr) / 10.0, 1),
            'cell_voltage_min': round(u16_5(base_addr + 2) / 1000.0, 3),
            'cell_voltage_max': round(u16_5(base_addr + 3) / 1000.0, 3),
            'temperature_min': round(u16_5(base_addr + 4) / 10.0, 1),
            'temperature_max': round(u16_5(base_addr + 5) / 10.0, 1),
            'soh': u16_5(base_addr + 15),
            'cycles': u16_5(base_addr + 16),
            'module_count': u16_5(base_addr + 19),
        }

    generator = {
        # All-zero on this shop's units - nothing is actually wired to the
        # smart/generator port right now, which is the honest, correct
        # reading (not a decoding bug).
        'power_a': s16_4(33530) * 10,
        'power_b': s16_4(33534) * 10,
        'power_c': s16_4(33535) * 10,
        'today_kwh': round(u16_4(33531) / 10.0, 1),
        'total_kwh': u32_4(33532),
        'smartport_voltage_a': round(u16_5(34328) / 10.0, 1),
        'smartport_voltage_b': round(u16_5(34329) / 10.0, 1),
        'smartport_voltage_c': round(u16_5(34330) / 10.0, 1),
        'smartport_current_a': round(u16_5(34331) / 10.0, 1),
        'smartport_current_b': round(u16_5(34332) / 10.0, 1),
        'smartport_current_c': round(u16_5(34333) / 10.0, 1),
    }
    battery_groups = [_decode_battery_group(34346), _decode_battery_group(34371)]
    # CONFIRMED (2026-09-06, live cross-check against SolisCloud with the
    # ports genuinely at different SOC - port 1 94%, port 2 98%): 34278 is
    # port 2's own real SOC (exact match), 34275 its own real voltage (exact
    # match, 587.4V) - and the "aggregate" battery.* fields read elsewhere
    # (33133 voltage, 33134 current, 33139 SOC, 33149 power, in
    # _solis_snapshot()'s main block) are actually port 1's own readings,
    # not a true aggregate. Port 1's values aren't available in this
    # function's own register block, so only port 2's group gets them set
    # here - _battery_stack_live_data() fills port 1's in from the main
    # block's reading instead of sharing port 2's or vice versa.
    battery_groups[1]['voltage'] = round(u16_6(34275) / 100.0, 1)
    # EXPERIMENTAL - only cross-checked at 0A/idle so far (see
    # SOLIS_BLOCK_BATTERY2_EXTRA's docstring); power is current x voltage,
    # not its own register, so it inherits the same confidence level as
    # current does.
    battery_groups[1]['current'] = round(s16_6(34276) / 10.0, 1)
    battery_groups[1]['power'] = round(battery_groups[1]['voltage'] * battery_groups[1]['current'])
    battery_groups[1]['direction'] = (
        'charge' if battery_groups[1]['current'] > 0 else
        'discharge' if battery_groups[1]['current'] < 0 else 'idle'
    )
    battery_groups[1]['soc'] = u16_6(34278)

    # Full per-phase grid meter reading - see SOLIS_BLOCK_METER_3P's
    # docstring for the /1000 (not /10) current scale correction and the
    # internal-consistency check that confirmed it. Direction sign follows
    # the SAME convention already established and used elsewhere in this
    # app for the inverter's own AC terminal power (admin_power.html's
    # "W активна мощност към мрежата (+ подава, - тегли)" - i.e. + =
    # exporting/feeding the grid, - = importing/drawing from it) rather
    # than the opposite, more common utility-meter convention - kept
    # consistent for the user's own sake even though this specific field
    # (a genuinely different measurement point, the external CT, not the
    # inverter's own terminal) hasn't been independently confirmed against
    # a real, clearly non-zero import or export event (this device read
    # ~0 net active power, mostly reactive, at the time this was decoded).
    # The separate cumulative energy_from_grid/energy_to_grid totals below
    # are unambiguous either way (each only ever counts up).
    active_total = s32_7(33263) / 10.0
    meter_3p = {
        'voltage_a': round(u16_7(33251) / 10.0, 1),
        'current_a': round(u16_7(33252) / 1000.0, 3),
        'voltage_b': round(u16_7(33253) / 10.0, 1),
        'current_b': round(u16_7(33254) / 1000.0, 3),
        'voltage_c': round(u16_7(33255) / 10.0, 1),
        'current_c': round(u16_7(33256) / 1000.0, 3),
        'active_power_a': round(s32_7(33257) / 10.0, 1),
        'active_power_b': round(s32_7(33259) / 10.0, 1),
        'active_power_c': round(s32_7(33261) / 10.0, 1),
        'active_power': round(active_total, 1),
        'reactive_power_a': round(s32_7(33265) / 10.0, 1),
        'reactive_power_b': round(s32_7(33267) / 10.0, 1),
        'reactive_power_c': round(s32_7(33269) / 10.0, 1),
        'reactive_power': round(s32_7(33271) / 10.0, 1),
        'apparent_power_a': round(s32_7(33273) / 10.0, 1),
        'apparent_power_b': round(s32_7(33275) / 10.0, 1),
        'apparent_power_c': round(s32_7(33277) / 10.0, 1),
        'apparent_power': round(s32_7(33279) / 10.0, 1),
        'power_factor': round(s16_7(33281) / 1000.0, 3),
        'frequency': round(u16_7(33282) / 100.0, 2),
        'energy_from_grid_kwh': round(u32_7(33283) / 1000.0, 2),
        'energy_to_grid_kwh': round(u32_7(33285) / 1000.0, 2),
        'direction': 'export' if active_total > 0 else 'import' if active_total < 0 else 'idle',
    }
    return generator, battery_groups, meter_3p


def _solis_extra_cached(device):
    now = time.time()
    with _solis_extra_cache_lock:
        cached = _solis_extra_cache.get(device.id)
    if cached and now - cached['ts'] < SOLIS_EXTRA_REFRESH_SECONDS:
        return cached['generator'], cached['battery_groups'], cached['meter_3p']
    try:
        generator, battery_groups, meter_3p = _solis_read_extra_blocks(device)
    except Exception:
        # Keep serving the last known-good value instead of blanking the UI
        # just because this slow-refresh read happened to fail once (e.g.
        # the gateway was mid-busy with the hot path) - same
        # don't-change-on-a-failed-check preference as the temperature
        # sensors' MQTT snapshot.
        if cached:
            return cached['generator'], cached['battery_groups'], cached['meter_3p']
        return None, None, None
    with _solis_extra_cache_lock:
        _solis_extra_cache[device.id] = {'ts': now, 'generator': generator, 'battery_groups': battery_groups, 'meter_3p': meter_3p}
    return generator, battery_groups, meter_3p


def _battery_stack_live_data(stack, source_snap):
    """
    Live SOC/voltage/temperature for a BatteryStack with source_type=
    'inverter' - just picks out source_snap['battery_groups'][0 or 1] (see
    _solis_read_extra_blocks()'s _decode_battery_group()), already fetched
    for stack.inverter_device_id elsewhere in the same poll pass by the
    caller (admin_battery_cabinets_data()/admin_power_data()/
    admin_factory_map_room_data()) - no Modbus read happens here.

    'soc'/'voltage'/'current'/'power'/'direction' are genuinely per-port
    (CONFIRMED 2026-09-06 for soc/voltage, see SOLIS_BLOCK_BATTERY2_EXTRA's
    docstring) - there was never a true combined/aggregate reading for any
    of these, just two independent per-port ones all along ("не показва
    коректно soc на батериите", "трябва да се вижда с какъв ток се
    зареждат/разреждат, каква мощност", "добавим в данните за батериите
    напрежението за всяка батерия"). Port 1's come from source_snap
    ['battery'] (registers 33139/33133/33134/33149, read in the main block
    so not present in groups[0] itself); port 2's are already baked into
    groups[1] by _solis_read_extra_blocks() (registers 34278/34275/34276,
    power derived as voltage*current). Also drives the battery icon's fill
    animation and in-icon SOC label on the map.

    None whenever there's nothing to show: source_type isn't 'inverter' (the
    only working option - see the model's docstring), inverter_device_id/
    bms_port aren't both set, or the inverter's own snapshot has no
    battery_groups (offline, or the 60s-cache read hasn't succeeded yet).
    """
    if stack.source_type != 'inverter' or not stack.inverter_device_id or stack.bms_port not in BATTERY_STACK_BMS_PORTS:
        return None
    if not source_snap or not source_snap.get('online') or not source_snap.get('battery_groups'):
        return None
    idx = 0 if stack.bms_port == '1' else 1
    groups = source_snap['battery_groups']
    if idx >= len(groups):
        return None
    result = dict(groups[idx])
    if idx == 0:
        battery = source_snap.get('battery') or {}
        result.update(soc=battery.get('soc'), voltage=battery.get('voltage'), current=battery.get('current'),
                       power=battery.get('power'), direction=battery.get('direction'))
    return result


def _battery_stack_snapshots(stacks):
    """Fetches each distinct source_type='inverter' inverter referenced by
    `stacks` exactly once, however many stacks share it (both BMS ports of
    the same inverter, or multiple pollers) - shared by every page that
    shows stack live data (admin_battery_cabinets_data()/admin_power_data()/
    admin_factory_map_room_data()). Returns {stack.id: live_data_or_None}."""
    inverter_ids = {s.inverter_device_id for s in stacks if s.source_type == 'inverter' and s.inverter_device_id}
    snap_by_inverter_id = {
        device.id: _solis_snapshot(device)
        for device in ModbusDevice.query.filter(ModbusDevice.id.in_(inverter_ids)).all()
    } if inverter_ids else {}
    return {s.id: _battery_stack_live_data(s, snap_by_inverter_id.get(s.inverter_device_id)) for s in stacks}


def _solis_snapshot(device):
    """
    Render-ready dict for a Solis S6 hybrid inverter - same top-level shape
    as shelly_device_snapshot()/_dtsu666_snapshot() (name/host/online/error/
    channels/total_power/total_energy/temperature), plus nested 'pv'/'ac'/
    'battery'/'load'/'meter'/'generator' blocks with everything the register
    map exposes (see SOLIS_BLOCK_* above) - admin_power.html renders these
    with a dedicated card layout instead of the generic channel grid.
    `channels` is always [] - a hybrid inverter's per-phase AC figures live
    in the 'ac' block instead, since they don't fit the Shelly/DTSU666
    per-channel shape (no per-channel power factor here). `total_power` is
    the AC active power at the grid port (+ exporting to grid, - importing).

    The inverter's 4 physical AC "ports" map onto existing/new blocks rather
    than a separate structure: Grid -> 'meter' (external CT), Основен/Main
    -> 'load.household_w', Backup -> 'load.backup_w', Generator/smart port
    -> the new 'generator' block. `battery_groups` is a separate,
    EXPERIMENTAL, best-effort decode of the two 11-module battery ports -
    see its assignment below for what that confidence level actually means.
    """
    try:
        b1_start, b1_count = SOLIS_BLOCK_ENERGY_PV_AC
        b3_start, b3_count = SOLIS_BLOCK_METER_BATTERY
        b1, b3 = _solis_read_blocks(device, [(b1_start, b1_count), (b3_start, b3_count)])

        u16_1 = lambda addr: _solis_u16(b1, b1_start, addr)
        s16_1 = lambda addr: _solis_s16(b1, b1_start, addr)
        u32_1 = lambda addr: _solis_u32(b1, b1_start, addr)
        s32_1 = lambda addr: _solis_s32(b1, b1_start, addr)
        u16_3 = lambda addr: _solis_u16(b3, b3_start, addr)
        s16_3 = lambda addr: _solis_s16(b3, b3_start, addr)
        u32_3 = lambda addr: _solis_u32(b3, b3_start, addr)
        s32_3 = lambda addr: _solis_s32(b3, b3_start, addr)

        dc_input_count = u16_1(33048) + 1  # 0 = 1 input, 1 = 2 inputs, ...
        pv_strings = [{
            'voltage': round(u16_1(33049 + idx * 2) / 10.0, 1),
            'current': round(u16_1(33050 + idx * 2) / 10.0, 1),
        } for idx in range(min(dc_input_count, 4))]

        ac_active_power = s32_1(33079)
        battery_power = s32_3(33149)
        direction = 'charge' if battery_power > 0 else 'discharge' if battery_power < 0 else 'idle'

        result = {
            'name': device.name, 'device_id': device.id, 'host': f'{device.host}:{device.port}', 'online': True, 'error': None,
            'channels': [], 'total_power': ac_active_power, 'total_energy': u32_1(33029),
            'temperature': round(s16_1(33093) / 10.0, 1),
            'pv': {
                'strings': pv_strings,
                'power': u32_1(33057),
                'today_kwh': round(u16_1(33035) / 10.0, 1),
                'yesterday_kwh': round(u16_1(33036) / 10.0, 1),
                'month_kwh': u32_1(33031),
                'total_kwh': u32_1(33029),
            },
            'ac': {
                'voltage_a': round(u16_1(33073) / 10.0, 1),
                'voltage_b': round(u16_1(33074) / 10.0, 1),
                'voltage_c': round(u16_1(33075) / 10.0, 1),
                'current_a': round(u16_1(33076) / 10.0, 1),
                'current_b': round(u16_1(33077) / 10.0, 1),
                'current_c': round(u16_1(33078) / 10.0, 1),
                'active_power': ac_active_power,
                'reactive_power': s32_1(33081),
                'apparent_power': s32_1(33083),
                'frequency': round(u16_1(33094) / 100.0, 2),
            },
            'battery': {
                'soc': u16_3(33139),
                'soh': u16_3(33140),
                'voltage': round(u16_3(33133) / 10.0, 1),
                'current': round(s16_3(33134) / 10.0, 1),
                # <1s, unsmoothed (33134 above is the smoothed reading) - reads
                # 0 when the battery DC/DC stage is off, not necessarily idle.
                'current_fast': round(s16_3(33217) / 10.0, 1),
                'power': battery_power,
                'direction': direction,
                'temperature': round(s16_1(33043) / 10.0, 1),
                'fault_bits': u16_3(33118),
                'total_charge_kwh': u32_3(33161),
                'total_discharge_kwh': u32_3(33165),
                'today_charge_kwh': round(u16_3(33163) / 10.0, 1),
                'today_discharge_kwh': round(u16_3(33167) / 10.0, 1),
            },
            'load': {
                'household_w': u16_3(33147),
                'backup_w': u16_3(33148),
            },
            'meter': {
                'voltage': round(u16_3(33128) / 10.0, 1),
                'current': round(u16_3(33129) / 10.0, 1),
                'power': s32_3(33130),
                'total_energy_kwh': round(u32_3(33126) / 1000.0, 2),
            },
        }
        # Generator/smart-port + the experimental battery-port breakdown are
        # deliberately NOT read here on every call - see _solis_extra_cached()'s
        # docstring for why (this hot path can be polled as often as once a
        # second per admin_power.html's refresh picker, times 2 inverters
        # sharing one gateway; the extra blocks are read at most once every
        # SOLIS_EXTRA_REFRESH_SECONDS regardless of poll frequency).
        result['generator'], result['battery_groups'], result['meter_3p'] = _solis_extra_cached(device)
    except Exception as e:
        return {
            'name': device.name, 'host': f'{device.host}:{device.port}', 'online': False,
            'error': str(e), 'channels': [], 'total_power': 0.0, 'total_energy': 0.0,
            'temperature': None, 'pv': None, 'ac': None, 'battery': None, 'load': None, 'meter': None,
            'generator': None, 'battery_groups': None, 'meter_3p': None,
        }

    return result


def _solis_grid_meter_view_snapshot(device, source_snap):
    """
    Standalone Consumption/map card for a ModbusDevice with
    device_type='solis_grid_meter' - a *virtual* row with no Modbus
    connection of its own (see its docstring on the model). Just reshapes
    source_snap['meter']/['meter_3p'] (the external grid CT physically
    wired into `device.source_device`'s own Modbus network - genuinely
    three-phase, explicit ask: "Виртуалния смарт метър... е трифазен.
    Записвай всички данни от него за всички фази") into the same
    name/host/online/error/channels/total_power/total_energy/temperature
    shape every other snapshot function returns, so it renders in
    admin_power.html and factory-map cards exactly like a real meter - one
    real per-phase channel each (same {label, voltage, current, act_power,
    aprt_power, pf, freq} shape as _dtsu666_snapshot()'s channels) instead
    of a single synthetic one, whenever meter_3p is available.

    'meter_3p' (see SOLIS_BLOCK_METER_3P in app.py) is also attached
    directly for callers that want the full per-phase/reactive/frequency/
    energy-import-export detail beyond what the generic channel shape
    carries - it's also what gets historically logged (SolisReadingLog.
    snapshot_json dumps the WHOLE snapshot dict every minute, meter_3p
    included, with zero extra logging code needed).

    source_snap is whatever the caller already fetched for source_device in
    this same poll pass (see admin_power_data()/_collect_power_aggregates())
    - this function does no Modbus I/O of its own, by design (reusing an
    already-fetched reading was the whole point, see ModbusDevice.
    source_device_id's docstring).
    """
    if not source_snap or not source_snap.get('online') or not source_snap.get('meter'):
        return {
            'name': device.name, 'host': f'{device.host}:{device.port}', 'online': False,
            'error': 'Няма данни от инвертора, през който се измерва.', 'channels': [],
            'total_power': 0.0, 'total_energy': 0.0, 'temperature': None, 'meter_3p': None,
        }
    m = source_snap['meter']
    m3 = source_snap.get('meter_3p')
    if m3:
        channels = [{
            'label': label, 'voltage': m3[f'voltage_{ph}'], 'current': m3[f'current_{ph}'],
            'act_power': m3[f'active_power_{ph}'], 'aprt_power': m3[f'apparent_power_{ph}'],
            'pf': None, 'freq': m3['frequency'],
        } for ph, label in (('a', 'Фаза A'), ('b', 'Фаза B'), ('c', 'Фаза C'))]
        # meter_3p's own 'active_power' (+import/-export, see its own
        # docstring on the UNCONFIRMED sign assumption) is used here instead
        # of the older single-value m['power'] - haven't independently
        # verified the two agree on sign/direction, and meter_3p is the one
        # carrying the explicit 'direction' the user asked for.
        total_power, total_energy = m3['active_power'], m3['energy_from_grid_kwh'] + m3['energy_to_grid_kwh']
    else:
        channels = [{'label': 'Мрежа', 'act_power': m['power'], 'voltage': m['voltage'], 'current': m['current'],
                      'aprt_power': None, 'pf': None, 'freq': None}]
        total_power, total_energy = m['power'], m['total_energy_kwh']
    return {
        'name': device.name, 'host': f'{device.host}:{device.port}', 'online': True, 'error': None,
        'channels': channels, 'total_power': total_power, 'total_energy': total_energy,
        'temperature': None, 'meter_3p': m3,
    }


@app.route('/admin/modbus-devices/create', methods=['POST'])
@role_required('admin')
def admin_add_modbus_device():
    """Adds a Modbus meter straight onto the Consumption page (/admin/power) -
    Modbus devices no longer have a standalone management page, they join
    the same list/live-dashboard ShellyDevice rows already use there."""
    name = request.form.get('name', '').strip()
    device_type = request.form.get('device_type', 'dtsu666')
    if device_type not in MODBUS_DEVICE_TYPES:
        device_type = 'dtsu666'
    if not name:
        flash('Моля въведете име.', 'danger')
        return redirect(url_for('admin_power'))
    machines = _resolve_machines_or_none(_parse_machine_ids(request.form))
    if machines is None:
        flash('Една от избраните машини не съществува.', 'danger')
        return redirect(url_for('admin_power'))
    panel_id_raw = request.form.get('panel_id', '')
    panel_id = int(panel_id_raw) if panel_id_raw.isdigit() and db.session.get(ElectricalPanel, int(panel_id_raw)) else None

    if device_type == 'solis_grid_meter':
        # Virtual row, no Modbus connection of its own - see
        # ModbusDevice.source_device_id's docstring. host/port/unit_id are
        # copied from the source purely for cosmetic display consistency
        # (never actually connected to for this type).
        source_device_id_raw = request.form.get('source_device_id', '')
        source_device = (
            db.session.get(ModbusDevice, int(source_device_id_raw))
            if source_device_id_raw.isdigit() else None
        )
        if not source_device or source_device.device_type != 'solis_s6':
            flash('Моля изберете инвертор, през който се измерва.', 'danger')
            return redirect(url_for('admin_power'))
        host, port, unit_id, source_device_id = source_device.host, source_device.port, source_device.unit_id, source_device.id
    else:
        host = request.form.get('host', '').strip()
        if not host:
            flash('Моля въведете IP адрес.', 'danger')
            return redirect(url_for('admin_power'))
        try:
            port = int(request.form.get('port', '502') or 502)
            unit_id = int(request.form.get('unit_id', '1') or 1)
        except ValueError:
            flash('Портът и Unit ID трябва да са числа.', 'danger')
            return redirect(url_for('admin_power'))
        source_device_id = None

    db.session.add(ModbusDevice(
        name=name, host=host, port=port, unit_id=unit_id, device_type=device_type, panel_id=panel_id, machines=machines,
        source_device_id=source_device_id, notes=request.form.get('notes', '').strip() or None,
    ))
    db.session.commit()
    log_action(f'Добавен Modbus електромер "{name}" ({host}:{port})')
    flash(f'Устройство "{name}" беше добавено.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/modbus-devices/<int:device_id>/update', methods=['POST'])
@role_required('admin')
def admin_update_modbus_device(device_id):
    """Full edit (name/host/port/unit id/notes/panel/machines) in one form -
    same field coverage as admin_power_rename_device() gives a ShellyDevice,
    so a Modbus row on /admin/power can be edited the same way as any other."""
    device = ModbusDevice.query.get_or_404(device_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Името не може да бъде празно.', 'danger')
        return redirect(url_for('admin_power'))
    machines = _resolve_machines_or_none(_parse_machine_ids(request.form))
    if machines is None:
        flash('Една от избраните машини не съществува.', 'danger')
        return redirect(url_for('admin_power'))
    device_type = request.form.get('device_type', device.device_type)
    if device_type not in MODBUS_DEVICE_TYPES:
        device_type = device.device_type
    panel_id_raw = request.form.get('panel_id', '')

    if device_type == 'solis_grid_meter':
        source_device_id_raw = request.form.get('source_device_id', '')
        source_device = (
            db.session.get(ModbusDevice, int(source_device_id_raw))
            if source_device_id_raw.isdigit() else None
        )
        if not source_device or source_device.device_type != 'solis_s6' or source_device.id == device.id:
            flash('Моля изберете инвертор, през който се измерва.', 'danger')
            return redirect(url_for('admin_power'))
        host, port, unit_id, source_device_id = source_device.host, source_device.port, source_device.unit_id, source_device.id
    else:
        host = request.form.get('host', '').strip()
        if not host:
            flash('IP адресът не може да бъде празен.', 'danger')
            return redirect(url_for('admin_power'))
        try:
            port = int(request.form.get('port', '502') or 502)
            unit_id = int(request.form.get('unit_id', '1') or 1)
        except ValueError:
            flash('Портът и Unit ID трябва да са числа.', 'danger')
            return redirect(url_for('admin_power'))
        source_device_id = None

    old_name = device.name
    device.name = name
    device.host = host
    device.port = port
    device.unit_id = unit_id
    device.device_type = device_type
    device.source_device_id = source_device_id
    device.notes = request.form.get('notes', '').strip() or None
    device.panel_id = int(panel_id_raw) if panel_id_raw.isdigit() and db.session.get(ElectricalPanel, int(panel_id_raw)) else None
    device.machines = machines
    db.session.commit()
    log_action(f'Редактиран Modbus електромер "{old_name}" → "{name}"')
    flash(f'Устройство "{name}" беше обновено.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/modbus-devices/<int:device_id>/ports', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_device_ports(device_id):
    """Sets which ElectricalPanel each of a Solis inverter's 4 AC ports
    (Мрежа/Основен/Бекъп/Генератор) connects to - a focused update touching
    only these 4 columns, same reasoning as admin_update_panel_room() (posting
    to the general admin_update_modbus_device() form would require resending
    every other field or lose them)."""
    device = ModbusDevice.query.get_or_404(device_id)

    def _panel_or_none(field):
        raw = request.form.get(field, '')
        return int(raw) if raw.isdigit() and db.session.get(ElectricalPanel, int(raw)) else None

    device.grid_panel_id = _panel_or_none('grid_panel_id')
    device.main_panel_id = _panel_or_none('main_panel_id')
    device.backup_panel_id = _panel_or_none('backup_panel_id')
    device.generator_panel_id = _panel_or_none('generator_panel_id')
    db.session.commit()
    log_action(f'Обновени портове на "{device.name}"')
    flash(f'Портовете на "{device.name}" бяха обновени.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/modbus-devices/<int:device_id>/delete', methods=['POST'])
@role_required('admin')
def admin_delete_modbus_device(device_id):
    device = ModbusDevice.query.get_or_404(device_id)
    name = device.name
    db.session.delete(device)
    db.session.commit()
    log_action(f'Изтрито Modbus устройство "{name}"')
    flash(f'Устройство "{name}" беше изтрито.', 'success')
    return redirect(url_for('admin_power'))


@app.route('/admin/modbus-devices/<int:device_id>/read', methods=['POST'])
@role_required('admin')
def admin_modbus_read_registers(device_id):
    """
    Diagnostic register dump - read N registers starting at a given address
    and show them as raw uint16 plus every plausible decoding, so a real
    reading (e.g. a voltage that should read ~230) can be matched by eye
    against the meter's own display. This is how the actual register map
    for a given meter gets confirmed - see ModbusDevice's docstring.
    """
    device = ModbusDevice.query.get_or_404(device_id)
    try:
        address = int(request.form.get('address', ''))
        count = int(request.form.get('count', '2'))
    except ValueError:
        return jsonify({'error': 'Невалиден адрес или брой регистри.'}), 400
    if count < 1 or count > 64:
        return jsonify({'error': 'Броят регистри трябва да е между 1 и 64.'}), 400
    input_type = 'input' if request.form.get('input_type') == 'input' else 'holding'

    registers, error = _modbus_read_raw(device.host, device.port, device.unit_id, address, count, input_type)
    if error:
        return jsonify({'error': error})

    return jsonify({
        'address': address, 'raw': registers,
        'uint16': _modbus_decode(registers, 'uint16'),
        'int16': _modbus_decode(registers, 'int16'),
        'uint32': _modbus_decode(registers, 'uint32') if count >= 2 else [],
        'int32': _modbus_decode(registers, 'int32') if count >= 2 else [],
        'float32': _modbus_decode(registers, 'float32') if count >= 2 else [],
    })


# ----------------- ИНТЕРАКТИВНА КАРТА НА ФАБРИКАТА -----------------
# Shop-floor map, one canvas per Room (Building -> Room -> Machine/Panel
# hierarchy - see Room's docstring for why the map isn't one single canvas).
# Every Machine/ElectricalPanel in a room can be placed on a schematic grid
# (a real floor-plan background can replace the grid later - pos_x/pos_y are
# percentages of the room's own canvas either way, so nothing about the
# position model has to change when that happens). Live power draw is
# pulled from whichever ShellyDevice(s) are linked to a Machine, or - for a
# panel - summed across every meter mounted inside it, reusing
# shelly_fleet_snapshot(), the same poll machinery /admin/power runs on.

# Below this, a reading is noise/standby draw, not real flow - mirrors the
# client-side POWER_ACTIVE_THRESHOLD_W in admin_factory_map_overview.html/
# admin_factory_map_room.html (kept as separate constants since one's
# Python and one's JS, but same value/reasoning).
POWER_ACTIVE_THRESHOLD_W = 5.0


def _collect_power_aggregates(machine_ok, panel_ok):
    """
    Shared by admin_factory_map_room_data() and admin_factory_map_overview_data():
    polls every ShellyDevice/ModbusDevice once and buckets live power into
    by_machine/by_panel dicts (keyed by id: {'online', 'total_power',
    'devices'}), including only machines/panels for which `machine_ok`/
    `panel_ok` return True - the room-scoped view passes "is this in room
    X", the site-wide overview passes "always" (a lambda returning True).
    A machine/panel with no linked meter simply gets no entry, same as
    machines.html shows for status alone.
    """
    by_machine = {}
    by_panel = {}

    def accumulate(device, snap):
        for machine in device.machines:
            if not machine_ok(machine):
                continue
            entry = by_machine.setdefault(machine.id, {'online': False, 'total_power': 0.0, 'devices': [], 'snapshots': []})
            entry['devices'].append(device.name)
            entry['online'] = entry['online'] or snap['online']
            entry['total_power'] += snap['total_power'] if snap['online'] else 0.0
            entry['snapshots'].append(snap)
        if device.panel_id and panel_ok(device.panel):
            entry = by_panel.setdefault(device.panel_id, {'online': False, 'total_power': 0.0, 'devices': [], 'snapshots': []})
            entry['devices'].append(device.name)
            entry['online'] = entry['online'] or snap['online']
            entry['total_power'] += snap['total_power'] if snap['online'] else 0.0
            # The full snapshot (channels / pv / ac / battery / load / meter -
            # whichever apply to this device kind) is passed through as-is, so
            # the factory map's hover tooltip can show the exact same detail
            # as /admin/power without a second round of Modbus reads. 'host'
            # is already on snap itself (used for the ?host= focus link).
            entry['snapshots'].append(snap)
            if snap.get('battery'):
                entry['battery'] = dict(snap['battery'], host=snap['host'])

    shelly_devices = ShellyDevice.query.order_by(ShellyDevice.id).all()
    # Zipped by position (shelly_fleet_snapshot preserves `devices`' order)
    # since a purely-MQTT device has no host to key by.
    for device, snap in zip(shelly_devices, shelly_fleet_snapshot(_shelly_snapshot_args(shelly_devices))):
        accumulate(device, snap)

    # 'solis_grid_meter' rows are virtual (see ModbusDevice.source_device_id) -
    # resolved in a second pass below from whatever was already fetched for
    # their source in the loop above, never polled directly.
    modbus_devices = ModbusDevice.query.all()
    snap_by_device_id = {}
    for device in modbus_devices:
        if device.device_type == 'solis_grid_meter':
            continue
        if (device.panel_id and panel_ok(device.panel)) or any(machine_ok(m) for m in device.machines):
            snap = _solis_snapshot(device) if device.device_type == 'solis_s6' else _dtsu666_snapshot(device)
            snap_by_device_id[device.id] = snap
            accumulate(device, snap)

    for device in modbus_devices:
        if device.device_type != 'solis_grid_meter':
            continue
        if not ((device.panel_id and panel_ok(device.panel)) or any(machine_ok(m) for m in device.machines)):
            continue
        source_snap = snap_by_device_id.get(device.source_device_id)
        if source_snap is None:
            continue
        accumulate(device, _solis_grid_meter_view_snapshot(device, source_snap))

    # A panel with no meter of its own (by_panel has no entry for it, since
    # accumulate() above only creates one from an actual attached device)
    # shows the sum of its nearest meter-equipped descendant panel(s)
    # instead, labeled "Преминаваща" (passthrough) - recurses past any
    # meterless panel in between, so a 2+-level-deep sub-panel's own meter
    # still surfaces at a meterless panel further up (e.g. a root panel
    # that's just a bus-bar with no meter of its own, only sub-panels that
    # actually have one). Machines aren't included - this is specifically
    # about the panel hierarchy (parent_panel_id).
    # Direction: an inverter (identifiable by its snapshot having a
    # 'battery' block, same as the connections' own reverse logic elsewhere)
    # is a generator - it feeds power up/out, so it counts as - in the net.
    # Everything else (a plain meter reading a panel's machines) is a
    # consumer and counts as + (draws power through). net < 0 means more is
    # being generated below than consumed, so the flow at this panel is
    # actually outward (e.g. towards the grid via a root panel's pole).
    def _descendant_meter_power(panel):
        net = 0.0
        online = False
        for child in panel.child_panels:
            if not panel_ok(child):
                continue
            entry = by_panel.get(child.id)
            if entry:
                if entry['online']:
                    online = True
                    if entry.get('battery') and entry['total_power'] > POWER_ACTIVE_THRESHOLD_W:
                        net -= entry['total_power']
                    else:
                        net += entry['total_power']
            else:
                sub_net, sub_online = _descendant_meter_power(child)
                net += sub_net
                online = online or sub_online
        return net, online

    for panel in ElectricalPanel.query.all():
        if not panel_ok(panel) or panel.id in by_panel:
            continue
        net, online = _descendant_meter_power(panel)
        by_panel[panel.id] = {
            'online': False, 'total_power': 0.0, 'devices': [], 'snapshots': [],
            'passthrough_power': abs(net), 'passthrough_online': online, 'passthrough_reverse': net < 0,
        }

    return by_machine, by_panel


@app.route('/admin/factory-map')
@role_required(['admin', 'worker'])
def admin_factory_map():
    """Room picker - the detailed map is drawn one room at a time (see
    admin_factory_map_room()); admin_factory_map_overview() is the site-wide
    panel-to-panel distribution diagram."""
    buildings = Building.query.order_by(Building.name).all()
    return render_template('admin_factory_map.html', buildings=buildings, active_page='admin_factory_map')


@app.route('/admin/factory-map/room/<int:room_id>')
@role_required(['admin', 'worker'])
def admin_factory_map_room(room_id):
    room = Room.query.get_or_404(room_id)
    machines = Machine.query.filter_by(room_id=room.id).order_by(Machine.id).all()
    panels = ElectricalPanel.query.filter_by(room_id=room.id).order_by(ElectricalPanel.id).all()
    convectors = Convector.query.filter_by(room_id=room.id).order_by(Convector.id).all()
    battery_stacks = BatteryStack.query.filter_by(room_id=room.id).order_by(BatteryStack.id).all()
    return render_template(
        'admin_factory_map_room.html', room=room, machines=machines, panels=panels, convectors=convectors,
        battery_stacks=battery_stacks, active_page='admin_factory_map'
    )


@app.route('/admin/factory-map/convector/<int:conv_id>/position', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_convector_position(conv_id):
    """Saves a convector's dragged (x, y) - see admin_update_machine_position()."""
    conv = Convector.query.get_or_404(conv_id)
    try:
        pos_x = float(request.form.get('pos_x', ''))
        pos_y = float(request.form.get('pos_y', ''))
    except ValueError:
        return jsonify({'error': 'Невалидна позиция.'}), 400

    conv.pos_x = max(0.0, min(100.0, pos_x))
    conv.pos_y = max(0.0, min(100.0, pos_y))
    db.session.commit()
    return jsonify({'pos_x': conv.pos_x, 'pos_y': conv.pos_y})


@app.route('/admin/factory-map/room/<int:room_id>/data')
@role_required(['admin', 'worker'])
def admin_factory_map_room_data(room_id):
    """JSON feed polled by admin_factory_map_room.html - live power per
    Machine/ElectricalPanel plus live on/off per Convector (each keyed by
    id) in this room."""
    room = Room.query.get_or_404(room_id)
    by_machine, by_panel = _collect_power_aggregates(
        lambda m: m.room_id == room.id, lambda p: p.room_id == room.id
    )
    convectors = Convector.query.filter_by(room_id=room.id).all()
    by_convector = {c.id: _shelly_convector_status(c) for c in convectors}
    # Only this room's own stacks - same reasoning as by_machine/by_panel
    # above (REMOTE_LINKS' 'stack' entries always key off the LOCAL stack,
    # never a remote one - see admin_factory_map_room.html).
    by_stack = _battery_stack_snapshots(BatteryStack.query.filter_by(room_id=room.id).all())
    return jsonify({
        'ts': datetime.now().strftime('%H:%M:%S'), 'machines': by_machine, 'panels': by_panel,
        'convectors': by_convector, 'stacks': by_stack,
    })


@app.route('/admin/factory-map/overview')
@role_required(['admin', 'worker'])
def admin_factory_map_overview():
    """
    Site-wide distribution diagram: every ElectricalPanel across every
    Building/Room, positioned on its own canvas (overview_pos_x/y - separate
    from each panel's position on its own room's map), connected by lines
    following parent_panel_id (which panel feeds which). This is the "main
    map" showing how the Building -> Room -> Panel sub-hierarchies relate to
    each other, as distinct from admin_factory_map_room() which shows one
    room's machines in physical-layout detail.
    """
    panels = ElectricalPanel.query.join(Room).join(Building).order_by(Building.name, Room.name, ElectricalPanel.name).all()
    # One dashed group box per room represented here (see ROOM_GROUPS/
    # redrawRoomGroups() in the template) - {'room': Room, 'panel_ids': [...]}
    # in first-appearance order, which is already building/room-name order
    # thanks to the query above.
    room_groups = []
    room_groups_by_id = {}
    for p in panels:
        entry = room_groups_by_id.get(p.room_id)
        if entry is None:
            entry = {'room': p.room, 'panel_ids': []}
            room_groups_by_id[p.room_id] = entry
            room_groups.append(entry)
        entry['panel_ids'].append(p.id)
    return render_template('admin_factory_map_overview.html', panels=panels, room_groups=room_groups, active_page='admin_factory_map')


@app.route('/admin/factory-map/overview/data')
@role_required(['admin', 'worker'])
def admin_factory_map_overview_data():
    """JSON feed polled by admin_factory_map_overview.html - live power per
    ElectricalPanel (keyed by id), site-wide (no room filter)."""
    _, by_panel = _collect_power_aggregates(lambda m: True, lambda p: True)
    return jsonify({'ts': datetime.now().strftime('%H:%M:%S'), 'panels': by_panel})


@app.route('/admin/factory-map/panel/<int:panel_id>/overview-position', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_overview_position(panel_id):
    """Saves a panel's dragged (x, y) on the site-wide overview canvas - see
    ElectricalPanel.overview_pos_x/y."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    try:
        pos_x = float(request.form.get('pos_x', ''))
        pos_y = float(request.form.get('pos_y', ''))
    except ValueError:
        return jsonify({'error': 'Невалидна позиция.'}), 400

    panel.overview_pos_x = max(0.0, min(100.0, pos_x))
    panel.overview_pos_y = max(0.0, min(100.0, pos_y))
    db.session.commit()
    return jsonify({'pos_x': panel.overview_pos_x, 'pos_y': panel.overview_pos_y})


@app.route('/admin/factory-map/machine/<int:machine_id>/position', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_machine_position(machine_id):
    """Saves a machine's dragged (x, y) as a percentage of its room's map
    canvas - see Machine.pos_x/pos_y."""
    machine = Machine.query.get_or_404(machine_id)
    try:
        pos_x = float(request.form.get('pos_x', ''))
        pos_y = float(request.form.get('pos_y', ''))
    except ValueError:
        return jsonify({'error': 'Невалидна позиция.'}), 400

    machine.pos_x = max(0.0, min(100.0, pos_x))
    machine.pos_y = max(0.0, min(100.0, pos_y))
    db.session.commit()
    return jsonify({'pos_x': machine.pos_x, 'pos_y': machine.pos_y})


@app.route('/admin/factory-map/panel/<int:panel_id>/position', methods=['POST'])
@role_required(['admin', 'worker'])
def admin_update_panel_position(panel_id):
    """Saves an electrical panel's dragged (x, y) - see admin_update_machine_position()."""
    panel = ElectricalPanel.query.get_or_404(panel_id)
    try:
        pos_x = float(request.form.get('pos_x', ''))
        pos_y = float(request.form.get('pos_y', ''))
    except ValueError:
        return jsonify({'error': 'Невалидна позиция.'}), 400

    panel.pos_x = max(0.0, min(100.0, pos_x))
    panel.pos_y = max(0.0, min(100.0, pos_y))
    db.session.commit()
    return jsonify({'pos_x': panel.pos_x, 'pos_y': panel.pos_y})


# Site coordinates (ТРАФКОМ ООД's own roof) for the sun-position compass on
# the roof map below - a Google Maps pin the user gave directly, not a guess.
SITE_LATITUDE = 42.9223667
SITE_LONGITUDE = 24.2480123


def _sun_position(dt_utc, lat_deg=SITE_LATITUDE, lon_deg=SITE_LONGITUDE):
    """
    Sun's azimuth (degrees, 0=North, clockwise - compass bearing) and
    altitude (degrees above the horizon, negative = below) for a naive-UTC
    datetime and site coordinates. Low-precision solar position formula
    (accurate to a small fraction of a degree - the standard approximation
    from Meeus' "Astronomical Algorithms", no ephemeris library needed) -
    plenty for a "where's the sun right now" compass widget.
    """
    rad = math.pi / 180
    d = (dt_utc - datetime(2000, 1, 1, 12, 0, 0)).total_seconds() / 86400.0

    mean_lon = (280.460 + 0.9856474 * d) % 360
    mean_anomaly = rad * ((357.528 + 0.9856003 * d) % 360)
    ecliptic_lon = rad * (mean_lon + 1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly))
    obliquity = rad * (23.439 - 0.0000004 * d)

    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_lon))
    right_ascension = math.degrees(math.atan2(math.cos(obliquity) * math.sin(ecliptic_lon), math.cos(ecliptic_lon))) % 360

    gmst = (280.46061837 + 360.98564736629 * d) % 360
    hour_angle = rad * ((gmst + lon_deg - right_ascension) % 360)

    lat = rad * lat_deg
    altitude = math.asin(math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(hour_angle))
    az_from_south = math.atan2(math.sin(hour_angle), math.cos(hour_angle) * math.sin(lat) - math.tan(declination) * math.cos(lat))
    azimuth = (math.degrees(az_from_south) + 180) % 360

    return {'azimuth': azimuth, 'altitude': math.degrees(altitude)}


def _sun_rise_set(dt_local_naive):
    """
    Returns (sunrise, sunset, path):
      - sunrise/sunset: {'time': 'HH:MM' (local), 'azimuth': degrees}, or
        None on a day with no sunrise/sunset (not reachable at this site's
        latitude, but harmless if SITE_LATITUDE/SITE_LONGITUDE ever change).
        Found by scanning _sun_position()'s altitude across the UTC day in
        15-minute steps and bisecting the -0.833 degree crossing (standard
        "sun's upper limb at the horizon" definition, correcting for
        atmospheric refraction) - reuses the exact same position formula as
        the live compass dot, rather than a second formula that could
        disagree with it at the edges.
      - path: today's above-horizon {azimuth, altitude} points (same
        15-minute samples), for the compass's dashed daylight-trajectory
        line.
    """
    HORIZON = -0.833
    utc_offset = dt_local_naive.astimezone().utcoffset()
    day_start_utc = datetime(dt_local_naive.year, dt_local_naive.month, dt_local_naive.day) - utc_offset
    samples = [day_start_utc + timedelta(minutes=15 * i) for i in range(97)]
    positions = [_sun_position(t) for t in samples]
    altitudes = [p['altitude'] for p in positions]

    def bisect(t_lo, t_hi, alt_hi):
        for _ in range(20):
            t_mid = t_lo + (t_hi - t_lo) / 2
            alt_mid = _sun_position(t_mid)['altitude']
            if (alt_mid > HORIZON) == (alt_hi > HORIZON):
                t_hi = t_mid
            else:
                t_lo = t_mid
        return t_lo + (t_hi - t_lo) / 2

    def describe(t):
        if t is None:
            return None
        return {'time': (t + utc_offset).strftime('%H:%M'), 'azimuth': round(_sun_position(t)['azimuth'], 1)}

    sunrise = sunset = None
    for i in range(len(samples) - 1):
        lo, hi = altitudes[i], altitudes[i + 1]
        if lo <= HORIZON < hi and sunrise is None:
            sunrise = bisect(samples[i], samples[i + 1], hi)
        elif lo > HORIZON >= hi and sunset is None:
            sunset = bisect(samples[i], samples[i + 1], hi)

    # Today's daylight path (for the compass's dashed trajectory line) -
    # same 15-minute samples already computed above for sunrise/sunset,
    # just kept wherever they're above the horizon, so the line and the
    # rise/set markers can never disagree with each other.
    path = [{'azimuth': round(p['azimuth'], 1), 'altitude': round(p['altitude'], 1)}
            for p in positions if p['altitude'] > HORIZON]

    return describe(sunrise), describe(sunset), path


# ----------------- ПОКРИВ СЪС СОЛАРНИ ПАНЕЛИ -----------------

@app.route('/admin/solar-roof')
@role_required('admin')
def admin_solar_roof():
    """
    Roof map: every SolarPanel grouped by inverter (one roof slope each),
    row by row, so admins can see and bulk-reassign which of the 4 DC
    inputs each physical module is wired into. See SolarPanel's docstring
    and migration/seed_solar_roof.py (the one-off script that generated the
    172 rows for this shop's real 56m x 13m roof).
    """
    inverters = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all()
    panels_by_inverter = {}
    for inv in inverters:
        panels_by_inverter[inv.id] = SolarPanel.query.filter_by(inverter_device_id=inv.id) \
            .order_by(SolarPanel.row, SolarPanel.col).all()
    return render_template('admin_solar_roof.html', inverters=inverters, panels_by_inverter=panels_by_inverter,
                           site_lat=SITE_LATITUDE, site_lon=SITE_LONGITUDE, active_page='admin_solar_roof')


@app.route('/admin/solar-roof/data')
@role_required('admin')
# The app-wide default limiter (300/hour per IP, meant for public/auth
# endpoints) was blocking this page's own 10s live poll after well under an
# hour open (360 req/hour on its own) - admins already have to be logged in
# to reach it, so exempt it rather than throttle a dashboard against itself.
@limiter.exempt
def admin_solar_roof_data():
    """
    Live power for the roof map's legend/tooltips, keyed
    "<inverter_device_id>-<string_number>" (1-8, matching SolarPanel.
    string_number). Each Solis MPPT tracker (_solis_snapshot()'s pv.strings,
    0-indexed, 4 entries) physically combines 2 wired-in-parallel strings
    into one measurement - the inverter has no way to see the two halves
    separately, so both of a tracker's string numbers (1&2, 3&4, 5&6, 7&8)
    report that same shared reading here, not an assumed 50/50 split.

    Also includes 'inverters' (keyed by device id) with each inverter's
    whole-array PV summary (current power, today/yesterday/month/lifetime
    kWh) for the "Анализ на слънцегреенето" section at the bottom of the
    roof map - built from the same snapshot already fetched for 'strings',
    not a second round of Modbus reads.
    """
    strings = {}
    inverter_summaries = {}
    for device in ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all():
        snap = _solis_snapshot(device)
        if snap['online']:
            for tracker_idx, s in enumerate(snap['pv']['strings']):
                reading = {
                    'voltage': s['voltage'], 'current': s['current'],
                    'power': round(s['voltage'] * s['current']),
                    'mppt': tracker_idx + 1,
                }
                strings[f'{device.id}-{tracker_idx * 2 + 1}'] = reading
                strings[f'{device.id}-{tracker_idx * 2 + 2}'] = reading
            inverter_summaries[device.id] = {
                'name': device.name, 'online': True, 'power': round(snap['pv']['power']),
                'today_kwh': snap['pv']['today_kwh'], 'yesterday_kwh': snap['pv']['yesterday_kwh'],
                'month_kwh': snap['pv']['month_kwh'], 'total_kwh': snap['pv']['total_kwh'],
            }
        else:
            inverter_summaries[device.id] = {'name': device.name, 'online': False}

    sun_now = _sun_position(datetime.utcnow())
    sunrise, sunset, sun_path = _sun_rise_set(datetime.now())
    sun_info = {
        'azimuth': round(sun_now['azimuth'], 1), 'altitude': round(sun_now['altitude'], 1),
        'is_day': sun_now['altitude'] > -0.833, 'sunrise': sunrise, 'sunset': sunset, 'path': sun_path,
    }
    return jsonify({'ts': datetime.now().strftime('%H:%M:%S'), 'strings': strings,
                    'inverters': inverter_summaries, 'sun': sun_info})


@app.route('/admin/solar-roof/assign', methods=['POST'])
@role_required('admin')
def admin_solar_roof_assign():
    """Bulk-assigns every selected panel to one string (or clears the
    assignment, if string_number is left blank) - the roof map's multi-
    select tool posts here once per "Присвои" click rather than one
    request per panel, since a real edit here is routinely 15-20+ panels
    at once (a whole string). 1-8: 4 MPPT trackers x 2 strings each - see
    admin_solar_roof_data()."""
    panel_ids = request.form.getlist('panel_ids')
    if not panel_ids:
        flash('Няма избрани панели.', 'danger')
        return redirect(url_for('admin_solar_roof'))
    string_raw = request.form.get('string_number', '')
    string_number = int(string_raw) if string_raw.isdigit() and 1 <= int(string_raw) <= 8 else None
    panels = SolarPanel.query.filter(SolarPanel.id.in_(panel_ids)).all()
    for p in panels:
        p.string_number = string_number
    db.session.commit()
    label = f'стринг {string_number}' if string_number else 'без стринг'
    log_action(f'Присвоени {len(panels)} соларни панела към {label}')
    flash(f'{len(panels)} панела бяха присвоени към {label}.', 'success')
    return redirect(url_for('admin_solar_roof'))


# ----------------- ОФЕРТИ (Offer generator) -----------------

@app.route('/admin/offers')
@role_required('admin')
def admin_offers():
    offers = Offer.query.order_by(Offer.created_at.desc()).all()
    return render_template('admin_offers.html', offers=offers, active_page='admin_offers')


@app.route('/api/upload-offer-item-image', methods=['POST'])
@role_required('admin')
def upload_offer_item_image():
    """Stages a photo for a not-yet-saved OfferItem line (product/detail rows
    only - the 'снимка' column in the shop's original offer spreadsheets),
    same stage-then-link pattern as upload_order_item_pdf(): saved
    immediately so the browser isn't holding the raw File object until the
    whole offer is submitted. _save_offer() links the staged file via
    _resolve_staged_offer_image(); an added-then-never-submitted row just
    leaves an orphaned file on disk, same tolerance as that PDF upload."""
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'status': 'error', 'message': 'Няма избран файл.'}), 400
    stored_filename = _save_upload(file, app.config['OFFER_IMAGES_FOLDER'], allowed_extensions=IMAGE_EXTENSIONS)
    if not stored_filename:
        return jsonify({'status': 'error', 'message': 'Разрешени са само изображения (png/jpg/jpeg/webp/gif).'}), 400
    return jsonify({
        'status': 'success',
        'filename': stored_filename,
        'url': url_for('static', filename=f'uploads/offers/{stored_filename}'),
    })


def _offer_picker_context():
    """Shared context for the offer create/edit form: catalog rows to pick
    from (as plain JSON-friendly dicts, for the add-item panel's JS) and the
    client list, same shape as order_create.html's cart picker."""
    products = [{'id': p.id, 'name': p.name, 'price': calculate_product_pricing(p)['sell_price']}
                for p in Product.query.order_by(Product.name).all()]
    details = [{'id': d.id, 'name': d.name, 'price': d.total_price} for d in Detail.query.order_by(Detail.name).all()]
    clients = Client.query.order_by(Client.name).all()
    return products, details, clients


def _offer_items_json(offer):
    """Serializes an existing Offer's items into the same plain-dict shape
    the editor's `cart` JS array uses, so admin_offer_edit.html can preload
    them for editing without a second round-trip."""
    if not offer:
        return []
    return [{
        'id': item.id, 'type': item.item_type, 'code': item.code or '', 'name': item.name or '',
        'description_html': item.description_html or '', 'dimensions': item.dimensions or '',
        'quantity': item.quantity, 'unit': item.unit or '', 'unit_price': item.unit_price,
        'image_filename': item.image_filename or '',
        'product_id': item.product_id, 'detail_id': item.detail_id,
    } for item in offer.items]


def _resolve_staged_offer_image(filename):
    """Validates a client-supplied image filename against OFFER_IMAGES_FOLDER
    - same re-check-don't-trust pattern as _link_order_item_attachment() for
    OrderItemAttachment's staged PDFs. Returns the filename only if it was
    actually staged there via upload_offer_item_image(), else None."""
    stored_filename = secure_filename(filename or '')
    if not stored_filename:
        return None
    if not os.path.isfile(os.path.join(app.config['OFFER_IMAGES_FOLDER'], stored_filename)):
        return None
    return stored_filename


def _save_offer(offer):
    """Creates (offer=None) or overwrites (offer=<existing Offer>) an Offer
    and its OfferItems from the submitted form - see admin_offer_edit.html's
    `cart` JS array serialized into items_json, same pattern as
    create_delivery_note()'s items_json. Editing replaces every item rather
    than diffing, since the whole cart is always resubmitted in full."""
    try:
        items = json.loads(request.form.get('items_json', ''))
        if not isinstance(items, list):
            items = []
    except (TypeError, ValueError):
        items = []

    if not items:
        flash('Добавете поне един артикул или текстов ред към офертата.', 'danger')
        return redirect(request.url)

    object_title = request.form.get('object_title', '').strip() or None
    client_id_raw = request.form.get('client_id', '')
    client_id = int(client_id_raw) if client_id_raw.isdigit() else None
    footer_notes = request.form.get('footer_notes', '').strip() or None
    signed_by = request.form.get('signed_by', '').strip() or None
    valid_until_raw = request.form.get('valid_until', '').strip()
    try:
        valid_until = datetime.strptime(valid_until_raw, '%Y-%m-%d').date() if valid_until_raw else None
    except ValueError:
        flash('Невалидна дата на валидност.', 'danger')
        return redirect(request.url)

    try:
        discount_percent = _parse_optional_float(request.form, 'discount_percent')
    except ValueError:
        flash('Невалидна отстъпка.', 'danger')
        return redirect(request.url)
    if discount_percent is not None and not (0 <= discount_percent <= 100):
        flash('Отстъпката трябва да бъде между 0 и 100%.', 'danger')
        return redirect(request.url)

    is_new = offer is None
    if is_new:
        offer = Offer(number=_next_offer_number(), created_by_id=current_user.id)
        db.session.add(offer)
    else:
        OfferItem.query.filter_by(offer_id=offer.id).delete()

    offer.object_title = object_title
    offer.client_id = client_id
    offer.valid_until = valid_until
    offer.discount_percent = discount_percent
    offer.footer_notes = footer_notes
    offer.signed_by = signed_by

    for position, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        item_type = row.get('type')
        if item_type not in ('product', 'detail', 'text'):
            continue

        def _optional_row_float(key):
            raw = row.get(key)
            try:
                return float(raw) if raw not in (None, '') else None
            except (TypeError, ValueError):
                return None

        description_html = sanitize_rich_text(row.get('description_html'))
        name = (row.get('name') or '').strip() or None
        code = (row.get('code') or '').strip() or None
        quantity = _optional_row_float('quantity')
        unit_price = _optional_row_float('unit_price')

        product_id = None
        detail_id = None
        if item_type in ('product', 'detail'):
            # A catalog pick must be a real quantity/price, same as before.
            if not name or quantity is None or quantity <= 0 or unit_price is None or unit_price < 0:
                continue
            unit = (row.get('unit') or '').strip() or 'бр'
            # Catalog id is optional even here - only set when the row came
            # from the product/detail picker (not a hand-edited name); used
            # solely by admin_offer_create_order() to turn the line back
            # into a real OrderItem.
            if item_type == 'product':
                product_id = row.get('product_id')
                product_id = int(product_id) if isinstance(product_id, (int, str)) and str(product_id).isdigit() else None
            else:
                detail_id = row.get('detail_id')
                detail_id = int(detail_id) if isinstance(detail_id, (int, str)) and str(detail_id).isdigit() else None
        else:
            # Free-typed row: every field is optional, but keep it only if
            # it carries *something* (a name, code, or descriptive text) -
            # a purely blank row is junk, not a note. Qty/price stay None
            # when left blank, so a pure text note still saves.
            if not (name or code or description_html):
                continue
            if (quantity is not None and quantity <= 0) or (unit_price is not None and unit_price < 0):
                continue
            unit = (row.get('unit') or '').strip() or None

        db.session.add(OfferItem(
            offer=offer, position=position, item_type=item_type,
            product_id=product_id, detail_id=detail_id,
            code=code, name=name, description_html=description_html,
            dimensions=(row.get('dimensions') or '').strip() or None,
            quantity=quantity, unit=unit, unit_price=unit_price,
            image_filename=_resolve_staged_offer_image(row.get('image_filename')),
        ))

    db.session.commit()
    offer_items = OfferItem.query.filter_by(offer_id=offer.id).order_by(OfferItem.position).all()
    item_lines = [
        f'{oi.name or "(текст)"}' + (f': {oi.quantity:g} {oi.unit or ""} x {oi.unit_price:g} лв.' if oi.quantity is not None and oi.unit_price is not None else '')
        for oi in offer_items
    ]
    log_action(f'{"Създадена" if is_new else "Обновена"} оферта № {offer.number} ("{offer.object_title or "-"}", {len(items)} артикул(и))',
               details=f'Оферта № {offer.number} ("{offer.object_title or "-"}")' + ('\n' + '\n'.join(item_lines) if item_lines else ''))
    flash(f'Офертата № {offer.number} беше запазена успешно.', 'success')
    return redirect(url_for('admin_offer_edit', offer_id=offer.id))


@app.route('/admin/offers/new', methods=['GET', 'POST'])
@role_required('admin')
def admin_offer_new():
    if request.method == 'POST':
        return _save_offer(None)
    products, details, clients = _offer_picker_context()
    return render_template('admin_offer_edit.html', offer=None, initial_items=[],
                            products=products, details=details, clients=clients, active_page='admin_offers')


@app.route('/admin/offers/<int:offer_id>/edit', methods=['GET', 'POST'])
@role_required('admin')
def admin_offer_edit(offer_id):
    offer = Offer.query.get_or_404(offer_id)
    if request.method == 'POST':
        return _save_offer(offer)
    products, details, clients = _offer_picker_context()
    return render_template('admin_offer_edit.html', offer=offer, initial_items=_offer_items_json(offer),
                            products=products, details=details, clients=clients, active_page='admin_offers')


@app.route('/admin/offers/<int:offer_id>/create-order', methods=['POST'])
@role_required('admin')
def admin_offer_create_order(offer_id):
    """Turns the checked product/detail lines of an existing Offer into a new
    Order, via the same product-recipe-snapshot / current-catalog-price logic
    as create_order()'s cart loop (component snapshot for products,
    detail.total_price - material plus the detail's own permanent operations -
    for standalone details) - offer lines don't carry per-detail ad-hoc
    operations/attachments, so those parts of that loop don't apply here.
    Text lines have no product_id/detail_id (see OfferItem) and can't be
    selected."""
    offer = Offer.query.get_or_404(offer_id)
    item_ids = request.form.getlist('item_ids', type=int)
    customer_name = request.form.get('customer_name', '').strip()
    if not customer_name:
        flash('Моля въведете име на клиент за новата поръчка.', 'danger')
        return redirect(url_for('admin_offer_edit', offer_id=offer.id))

    selected = [oi for oi in offer.items if oi.id in item_ids and (oi.product_id or oi.detail_id)]
    if not selected:
        flash('Изберете поне един ред (продукт/детайл) от офертата.', 'danger')
        return redirect(url_for('admin_offer_edit', offer_id=offer.id))

    new_order = Order(order_number=generate_order_number(), user_id=current_user.id,
                       customer_name=customer_name, status='new', client_id=offer.client_id)
    db.session.add(new_order)
    db.session.flush()

    added_any = False
    for oi in selected:
        # The offer's own quantity is just the default - the order-creation
        # panel lets an admin tweak it per line before creating the order
        # (e.g. quoted 2000 but only ordering 500 right now), via a
        # `qty_<offer_item_id>` field alongside each checked item_id. Falls
        # back to the offer's quantity when that field is missing/invalid.
        qty_override = request.form.get(f'qty_{oi.id}', '')
        try:
            qty = int(float(qty_override)) if qty_override else 0
        except ValueError:
            qty = 0
        if qty < 1:
            qty = int(oi.quantity) if oi.quantity and oi.quantity >= 1 else 1
        if oi.product_id:
            product = Product.query.get(oi.product_id)
            if not product:
                continue
            pricing = calculate_product_pricing(product)
            order_item = OrderItem(order_id=new_order.id, product_id=product.id,
                                    quantity_ordered=qty, unit_price=pricing['sell_price'])
            db.session.add(order_item)
            db.session.flush()
            for pd in product.product_details:
                db.session.add(OrderItemComponent(
                    order_item_id=order_item.id, detail_id=pd.detail_id,
                    detail_name_snapshot=pd.detail.name, quantity_needed=pd.quantity * qty
                ))
            added_any = True
        elif oi.detail_id:
            detail = Detail.query.get(oi.detail_id)
            if not detail:
                continue
            db.session.add(OrderItem(order_id=new_order.id, detail_id=detail.id,
                                      quantity_ordered=qty, unit_price=detail.total_price))
            added_any = True

    if not added_any:
        db.session.rollback()
        flash('Избраните редове вече не съответстват на съществуващи продукти/детайли.', 'danger')
        return redirect(url_for('admin_offer_edit', offer_id=offer.id))

    db.session.commit()
    log_action(f'Създадена поръчка {new_order.order_number} от оферта № {offer.number} за "{customer_name}" ({len(selected)} артикул(и))')
    flash(f'Поръчка {new_order.order_number} беше създадена от офертата.', 'success')
    return redirect(url_for('admin_production_report'))


@app.route('/admin/offers/<int:offer_id>/delete', methods=['POST'])
@role_required('admin')
def admin_offer_delete(offer_id):
    offer = Offer.query.get_or_404(offer_id)
    number = offer.number
    db.session.delete(offer)
    db.session.commit()
    log_action(f'Изтрита оферта № {number}')
    flash(f'Офертата № {number} беше изтрита.', 'success')
    return redirect(url_for('admin_offers'))


@app.route('/admin/offers/<int:offer_id>/duplicate', methods=['POST'])
@role_required('admin')
def admin_offer_duplicate(offer_id):
    """
    Copies an existing Offer and every OfferItem into a brand-new Offer with
    its own auto-generated number (see _next_offer_number()) - a quick way
    to reuse a past quote as the starting point for a new one instead of
    rebuilding it line by line. Photos aren't re-uploaded, just referenced by
    the same image_filename as the source item - harmless, since nothing in
    this app ever deletes an offer photo file off disk (same
    orphaned-file tolerance as upload_offer_item_image()/
    upload_order_item_pdf()).
    """
    source = Offer.query.get_or_404(offer_id)
    new_offer = Offer(
        number=_next_offer_number(), object_title=source.object_title, client_id=source.client_id,
        footer_notes=source.footer_notes, signed_by=source.signed_by, valid_until=source.valid_until,
        discount_percent=source.discount_percent, created_by_id=current_user.id,
    )
    db.session.add(new_offer)
    db.session.flush()
    for item in source.items:
        db.session.add(OfferItem(
            offer_id=new_offer.id, position=item.position, item_type=item.item_type,
            code=item.code, name=item.name, description_html=item.description_html,
            dimensions=item.dimensions, quantity=item.quantity, unit=item.unit,
            unit_price=item.unit_price, image_filename=item.image_filename,
        ))
    db.session.commit()
    log_action(f'Дублирана оферта № {source.number} → № {new_offer.number}')
    flash(f'Офертата беше дублирана като № {new_offer.number}.', 'success')
    return redirect(url_for('admin_offer_edit', offer_id=new_offer.id))


@app.route('/admin/offers/<int:offer_id>/print')
@role_required('admin')
def admin_offer_print(offer_id):
    """
    Browser-print view of an Offer - same pattern as offer.html/protocol.html/
    certificate.html (see CLAUDE.md's 'Offer / protocol / certificate
    documents' section): @media print CSS, no server-side PDF library. The
    user hits the page's 'Печат / PDF' button (window.print()) and saves as
    PDF from the browser's print dialog. description_html is rendered with
    |safe since it's already allowlist-sanitized to bold/italic/<br> only
    (see sanitize_rich_text()) - safe to trust here, unlike raw user input.
    """
    offer = Offer.query.get_or_404(offer_id)
    return render_template('admin_offer_print.html', offer=offer)


# ----------------- CHAT WIDGET -----------------
# Public FAQ bot for the floating widget in footer.html. Stateless - the
# browser resends its own running history each turn, nothing is persisted
# server-side. Deliberately answers general questions from live DB lookups
# (see CHATBOT_TOOLS below) rather than a static text blob, but real DXF
# prices still only ever come from the calculator (analyze_dxf_geometry/
# calculate_cnc_price) - the prompt forbids the model from guessing one.
CHATBOT_SYSTEM_PROMPT = """Ти си виртуален асистент на сайта на Трафком ООД - фирма за прецизна ЦПУ (лазерна/CNC) обработка на метал в гр. Тетевен, България, основана през 1997 г.

Основна информация:
- Адреси: ул. „Александър Стамболийски“ 36, гр. Тетевен (офис) и ул. „Воловийте“, 5700 гр. Тетевен (производствена база).
- Телефон: +359 886 762 276
- Имейл: trafcom@trafcombg.com
- Сайтът предлага DXF CNC Калкулатор (клиентът качва DXF чертеж и получава автоматична цена на база площ, дължина на рязане и брой пробождания) и Параметричен Генератор на форми.

Разговаряш само с вписани (логнати) потребители - имаш достъп до инструменти за да провериш реални, актуални данни: каталога на фирмата (материали, детайли, продукти, наличност на склад, типове материали), машини и услуги (вкл. цени), най-скъпи артикули, собствените поръчки на текущия потребител (номер, статус, недостиг на наличност), собствената му DXF библиотека с качвания, и основния му профил. Използвай инструментите, вместо да гадаеш, когато въпросът е за нещо, което те биха проверили. Имаш и инструмент за изпълнение на код за математически изчисления (виж правило 4).

Правила, които спазваш стриктно:
1. Отговаряй кратко, любезно и на български език. Никога не използвай emoji.
2. НИКОГА не измисляй конкретна цена - реалната цена зависи от геометрията на чертежа и се смята автоматично само след качване на DXF файл. За цена винаги насочвай клиента към DXF калкулатора на сайта или към контакт с офиса.
3. Ако инструментите не намерят отговор или въпросът е извън тяхната информация (срокове, индивидуални условия), кажи го честно и насочи клиента към телефона или имейла по-горе - не измисляй факти.
4. Не давай съвети извън темата на фирмата - с едно изключение: ако потребителят помоли за математическо изчисление (дори несвързано с Трафком), реши го точно, като ползваш code execution инструмента за всичко по-сложно от наум смятане.
5. order_status/my_orders винаги връщат само поръчки на текущия потребител - никога не твърди, че виждаш поръчка на друг клиент."""


def _fuzzy_match(rows, name_of, query, limit=5, cutoff=0.6):
    """
    Ranks rows against a search query - substring hits always win (score
    1.0), typo'd/misspelled queries fall back to a per-word difflib ratio.
    Catalog tables are small (tens of rows), so scoring every row in Python
    on each call is simpler than adding a DB-side fuzzy-search extension
    (e.g. Postgres pg_trgm) - revisit only if the catalog grows enough that
    this becomes a real cost.
    """
    query_lower = query.lower().strip()
    scored = []
    for row in rows:
        name_lower = name_of(row).lower()
        if query_lower in name_lower:
            scored.append((1.0, row))
            continue
        best = max(
            (difflib.SequenceMatcher(None, query_lower, word).ratio() for word in name_lower.split()),
            default=0.0,
        )
        if best >= cutoff:
            scored.append((best, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:limit]]


@anthropic.beta_tool
def search_catalog(query: str) -> str:
    """Търси в каталога на Трафком (материали, детайли, продукти) по име или марка - толерира леки правописни грешки - и връща наличност на склад.

    Args:
        query: Дума или част от име за търсене, напр. "алуминий" или "панел".
    """
    query = (query or '').strip()
    if not query:
        return 'Няма подадена дума за търсене.'
    results = []
    for m in _fuzzy_match(MaterialPrice.query.all(), lambda x: x.display_name, query, limit=5):
        results.append(f'Материал: {format_material_option(m)} - наличност: {m.stock_quantity:g}')
    for d in _fuzzy_match(Detail.query.all(), lambda x: x.name, query, limit=5):
        results.append(f'Детайл: {d.name} - наличност: {d.stock_quantity:g} бр.')
    for p in _fuzzy_match(Product.query.all(), lambda x: x.name, query, limit=5):
        results.append(f'Продукт: {p.name} - наличност: {p.stock_quantity:g} бр.')
    if not results:
        return f"Няма намерени резултати за '{query}' в каталога."
    return '\n'.join(results)


@anthropic.beta_tool
def list_services() -> str:
    """Връща списъка с машините и услугите на Трафком от страница „Услуги“."""
    cards = ServiceMachineCard.query.filter_by(page='services').all()
    if not cards:
        return 'В момента няма въведени услуги.'
    lines = []
    for c in cards:
        desc = (c.description or '').strip()
        lines.append(f'{c.title}: {desc[:200]}' if desc else c.title)
    return '\n'.join(lines)


@anthropic.beta_tool
def most_expensive_items(limit: int = 5) -> str:
    """Връща най-скъпите материали и детайли в каталога, подредени по цена низходящо - за въпроси от типа "кое е най-скъпото нещо на склад".

    Args:
        limit: Максимален брой резултати за всяка категория (по подразбиране 5, максимум 20).
    """
    limit = max(1, min(limit, 20))
    results = []
    for m in MaterialPrice.query.order_by(MaterialPrice.cost_per_m2.desc()).limit(limit):
        results.append(f'Материал: {format_material_option(m)} - {m.cost_per_m2:g} €/м² - наличност: {m.stock_quantity:g}')
    for d in Detail.query.order_by(Detail.calculated_price.desc()).limit(limit):
        results.append(f'Детайл: {d.name} - {d.calculated_price:g} € - наличност: {d.stock_quantity:g} бр.')
    if not results:
        return 'Няма данни в каталога.'
    return '\n'.join(results)


_PAGE_SIZE = 30


@anthropic.beta_tool
def list_materials(material_type: str = '', offset: int = 0) -> str:
    """Изброява материалите в каталога, по избор филтрирани по тип - за разглеждане, когато клиентът не знае точното име.

    Args:
        material_type: По избор - един от: sheets, rods, profiles, pipes, other. Празно за всички типове.
        offset: Колко записа да пропусне - за следваща страница при повече от 30 резултата.
    """
    q = MaterialPrice.query
    material_type = (material_type or '').strip()
    if material_type:
        q = q.filter_by(type=material_type)
    q = q.order_by(MaterialPrice.display_name)
    total = q.count()
    offset = max(0, offset)
    materials = q.offset(offset).limit(_PAGE_SIZE).all()
    if not materials:
        return 'Няма намерени материали от този тип.' if offset == 0 else 'Няма повече резултати.'
    lines = [f'{format_material_option(m)} - наличност: {m.stock_quantity:g}' for m in materials]
    if total > offset + len(materials):
        lines.append(f'(Показани {offset + 1}-{offset + len(materials)} от общо {total} - за следващите извикай отново с offset={offset + _PAGE_SIZE}.)')
    return '\n'.join(lines)


@anthropic.beta_tool
def get_material_details(name: str) -> str:
    """Връща пълните характеристики на конкретен материал по име (толерира леки правописни грешки).

    Args:
        name: Име или част от името на материала.
    """
    matches = _fuzzy_match(MaterialPrice.query.all(), lambda x: x.display_name, name, limit=1)
    if not matches:
        return f"Не намерих материал с име '{name}'."
    material = matches[0]
    return (
        f'{format_material_option(material)}\n'
        f'Тип: {MATERIAL_TYPE_LABELS.get(material.type, material.type)}\n'
        f'Цена: {material.cost_per_m2:g} €/м²\n'
        f'Наличност: {material.stock_quantity:g}'
    )


@anthropic.beta_tool
def list_products(offset: int = 0) -> str:
    """Изброява продуктите в каталога с цена на продажба и наличност.

    Args:
        offset: Колко записа да пропусне - за следваща страница при повече от 30 резултата.
    """
    q = Product.query.order_by(Product.name)
    total = q.count()
    offset = max(0, offset)
    products = q.offset(offset).limit(_PAGE_SIZE).all()
    if not products:
        return 'В момента няма въведени продукти.' if offset == 0 else 'Няма повече резултати.'
    lines = []
    for p in products:
        price = calculate_product_pricing(p)['sell_price']
        lines.append(f'{p.name} - {price:g} € - наличност: {p.stock_quantity:g} бр.')
    if total > offset + len(products):
        lines.append(f'(Показани {offset + 1}-{offset + len(products)} от общо {total} - за следващите извикай отново с offset={offset + _PAGE_SIZE}.)')
    return '\n'.join(lines)


@anthropic.beta_tool
def get_product_details(name: str) -> str:
    """Връща състава (от какви детайли е направен) и цената на конкретен продукт по име (толерира леки правописни грешки).

    Args:
        name: Име или част от името на продукта.
    """
    matches = _fuzzy_match(Product.query.all(), lambda x: x.name, name, limit=1)
    if not matches:
        return f"Не намерих продукт с име '{name}'."
    product = matches[0]
    pricing = calculate_product_pricing(product)
    lines = [f'{product.name} - цена: {pricing["sell_price"]:g} € - наличност: {product.stock_quantity:g} бр.']
    if product.description:
        lines.append(product.description.strip())
    lines.append('Съставни детайли:')
    for pd in product.product_details:
        lines.append(f'- {pd.detail.name} x{pd.quantity}')
    return '\n'.join(lines)


@anthropic.beta_tool
def my_orders(offset: int = 0) -> str:
    """Изброява собствените поръчки на текущия логнат потребител (номер, статус, дата, обща цена).

    Args:
        offset: Колко записа да пропусне - за следваща страница при повече от 15 поръчки.
    """
    q = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc())
    total = q.count()
    offset = max(0, offset)
    page_size = 15
    orders = q.offset(offset).limit(page_size).all()
    if not orders:
        return 'Нямате направени поръчки.' if offset == 0 else 'Няма повече резултати.'
    lines = [
        f'№ {o.order_number} - {o.status_label} ({o.percent_complete:g}%) - '
        f'{o.created_at.strftime("%d.%m.%Y")} - {o.total_price:g} €'
        for o in orders
    ]
    if total > offset + len(orders):
        lines.append(f'(Показани {offset + 1}-{offset + len(orders)} от общо {total} - за следващите извикай отново с offset={offset + page_size}.)')
    return '\n'.join(lines)


@anthropic.beta_tool
def order_status(order_number: str) -> str:
    """Връща детайли за конкретна поръчка на текущия логнат потребител по номер - никога на друг клиент.

    Args:
        order_number: Номерът на поръчката, напр. "2026-0042".
    """
    order = Order.query.filter_by(order_number=order_number, user_id=current_user.id).first()
    if not order:
        return f"Нямате поръчка с номер '{order_number}'."
    lines = [f'№ {order.order_number} - {order.status_label} - {order.percent_complete:g}% завършена - {order.total_price:g} €']
    for item in order.items:
        item_name = item.product.name if item.product else item.detail.name
        lines.append(f'- {item_name} x{item.quantity_ordered}')
    return '\n'.join(lines)


@anthropic.beta_tool
def order_missing_stock(order_number: str) -> str:
    """Проверява дали има недостиг на наличност за конкретна поръчка на текущия потребител - дали може да бъде изпълнена веднага.

    Args:
        order_number: Номерът на поръчката.
    """
    order = Order.query.filter_by(order_number=order_number, user_id=current_user.id).first()
    if not order:
        return f"Нямате поръчка с номер '{order_number}'."
    shortfalls = order_missing_items(order)
    if not shortfalls:
        return f'Поръчка № {order.order_number} има пълна наличност за всички артикули.'
    lines = [f'Поръчка № {order.order_number} има недостиг по следните артикули:']
    for s in shortfalls:
        lines.append(f"- {s['item_name']}: нужни {s['needed']}, налични {s['available']}, липсват {s['missing']}")
    return '\n'.join(lines)


@anthropic.beta_tool
def list_service_prices() -> str:
    """Връща списък с публичните цени на услугите (€/час или €/метър) на Трафком."""
    services = Service.query.filter_by(show_price=True).order_by(Service.name).all()
    if not services:
        return 'В момента няма публикувани цени на услуги.'
    lines = []
    for s in services:
        if s.pricing_mode == 'length' and s.price_per_meter_eur is not None:
            lines.append(f'{s.name}: {s.price_per_meter_eur:g} €/м')
        else:
            lines.append(f'{s.name}: {s.price_per_hour_eur:g} €/час')
    return '\n'.join(lines)


@anthropic.beta_tool
def get_machine_details(name: str) -> str:
    """Връща спецификации и описание за конкретна машина по име (толерира леки правописни грешки).

    Args:
        name: Име или част от името на машината.
    """
    matches = _fuzzy_match(ServiceMachineCard.query.all(), lambda x: x.title, name, limit=1)
    if not matches:
        return f"Не намерих машина с име '{name}'."
    card = matches[0]
    lines = [card.title]
    if card.specs_text:
        lines.append(card.specs_text.strip())
    if card.description:
        lines.append(card.description.strip())
    return '\n'.join(lines)


@anthropic.beta_tool
def list_material_types() -> str:
    """Връща възможните типове материали (за филтриране в list_materials)."""
    return '\n'.join(f'{key} - {label}' for key, label in MATERIAL_TYPE_LABELS.items())


@anthropic.beta_tool
def my_uploads(offset: int = 0) -> str:
    """Изброява предишните DXF качвания на текущия потребител от личната му библиотека (файл, материал, изчислена цена).

    Args:
        offset: Колко записа да пропусне - за следваща страница.
    """
    q = DxfFile.query.filter_by(user_id=current_user.id).order_by(DxfFile.id.desc())
    total = q.count()
    offset = max(0, offset)
    page_size = 15
    uploads = q.offset(offset).limit(page_size).all()
    if not uploads:
        return 'Нямате качени DXF файлове.' if offset == 0 else 'Няма повече резултати.'
    lines = [f'{u.filename} - {u.material} - {u.calculated_price:g} €' for u in uploads]
    if total > offset + len(uploads):
        lines.append(f'(Показани {offset + 1}-{offset + len(uploads)} от общо {total} - за следващите извикай отново с offset={offset + page_size}.)')
    return '\n'.join(lines)


@anthropic.beta_tool
def get_upload_details(query: str) -> str:
    """Връща подробности за конкретно DXF качване на текущия потребител по име на файла (толерира леки правописни грешки).

    Args:
        query: Част от името на файла.
    """
    matches = _fuzzy_match(DxfFile.query.filter_by(user_id=current_user.id).all(), lambda x: x.filename, query, limit=1)
    if not matches:
        return f"Не намерих качване с име '{query}'."
    u = matches[0]
    return (
        f'{u.filename}\n'
        f'Материал: {u.material}\n'
        f'Размери: {u.width:g} x {u.height:g} мм\n'
        f'Дължина на рязане: {u.total_length:g} мм\n'
        f'Цена: {u.calculated_price:g} €'
    )


@anthropic.beta_tool
def my_profile() -> str:
    """Връща основната информация за акаунта на текущия логнат потребител."""
    role_labels = {'regular_user': 'Клиент', 'worker': 'Служител', 'admin': 'Администратор', 'web_designer': 'Уеб дизайнер', 'quality_control': 'Контрол на качеството'}
    lines = [f'Потребителско име: {current_user.username}', f'Роля: {role_labels.get(current_user.role, current_user.role)}']
    if current_user.email:
        verified = 'потвърден' if current_user.email_verified else 'непотвърден'
        lines.append(f'Имейл: {current_user.email} ({verified})')
    return '\n'.join(lines)


@anthropic.beta_tool
def get_contact_info() -> str:
    """Връща адрес, телефон и имейл за контакт с Трафком."""
    return (
        'Адреси: ул. „Александър Стамболийски“ 36, гр. Тетевен (офис) и '
        'ул. „Воловийте“, 5700 гр. Тетевен (производствена база)\n'
        'Телефон: +359 886 762 276\n'
        'Имейл: trafcom@trafcombg.com'
    )


CHATBOT_TOOLS = [
    search_catalog, list_services, most_expensive_items,
    list_materials, get_material_details, list_products, get_product_details,
    my_orders, order_status, order_missing_stock,
    list_service_prices, get_machine_details, list_material_types,
    my_uploads, get_upload_details, my_profile, get_contact_info,
    # Server-side tool - runs in Anthropic's own sandbox, not on this server,
    # so it never touches the DB/filesystem. Pure computation only (see rule
    # 4 in CHATBOT_SYSTEM_PROMPT) - unrelated to the read-only site tools above.
    {'type': 'code_execution_20260521', 'name': 'code_execution'},
]


@app.route('/api/chat', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def api_chat():
    if anthropic_client is None:
        return jsonify({'status': 'error', 'message': 'Чатът временно не е достъпен.'}), 503

    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'status': 'error', 'message': 'Съобщението е празно.'}), 400
    if len(user_message) > 1000:
        return jsonify({'status': 'error', 'message': 'Съобщението е твърде дълго.'}), 400

    # Cap what we trust from the client-supplied history so a malicious
    # payload can't inflate the request into a huge/expensive one.
    raw_history = data.get('history')
    history = []
    if isinstance(raw_history, list):
        for entry in raw_history[-10:]:
            if not isinstance(entry, dict):
                continue
            role = entry.get('role')
            content = (entry.get('content') or '')[:1000]
            if role in ('user', 'assistant') and content:
                history.append({'role': role, 'content': content})

    try:
        runner = anthropic_client.beta.messages.tool_runner(
            model='claude-haiku-4-5',
            max_tokens=1024,
            max_iterations=4,  # caps tool-call round-trips per message - cost/abuse guard
            system=[{
                'type': 'text',
                'text': CHATBOT_SYSTEM_PROMPT,
                'cache_control': {'type': 'ephemeral'},
            }],
            tools=CHATBOT_TOOLS,
            messages=history + [{'role': 'user', 'content': user_message}],
        )
        last = None
        for message in runner:
            last = message
    except anthropic.APIError:
        return jsonify({'status': 'error', 'message': 'Възникна грешка при връзката с асистента. Опитайте отново.'}), 502

    reply = next((b.text for b in last.content if b.type == 'text'), '') if last else ''
    return jsonify({'status': 'ok', 'reply': reply})


# ----------------- CUSTOM ERROR PAGES -----------------
# One shared template (error.html), parameterized per status code, instead
# of a near-duplicate page per code.
@app.errorhandler(404)
def handle_404(e):
    return render_template('error.html', code=404, title=gettext('Страницата не е намерена'),
                            message=gettext('Проверете адреса или се върнете към началото.')), 404


@app.errorhandler(403)
def handle_403(e):
    return render_template('error.html', code=403, title=gettext('Нямате достъп'),
                            message=gettext('Нямате права за тази страница.')), 403


@app.errorhandler(429)
def handle_429(e):
    return render_template('error.html', code=429, title=gettext('Твърде много опити'),
                            message=gettext('Изчакайте малко и опитайте отново.')), 429


@app.errorhandler(500)
def handle_500(e):
    return render_template('error.html', code=500, title=gettext('Възникна грешка'),
                            message=gettext('Нещо се обърка от наша страна. Опитайте отново по-късно.')), 500


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    # Expired/missing CSRF token - most often a form left open too long
    # across a login-session boundary, not an attack in the normal case.
    return render_template('error.html', code=400, title=gettext('Изтекла сесия на формата'),
                            message=gettext('Презаредете страницата и опитайте отново.')), 400


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
        # Same pattern for the billable Services catalog (hourly rates the
        # pricing engine needs - see calculate_cnc_price()).
        seed_billable_services()
        # Same pattern for the services page's machine-park cards - only runs
        # if ServiceMachineCard is completely empty (see seed_service_machine_cards).
        seed_service_machine_cards()
        seed_index_machine_cards()
        # One-time migration of the legacy SHELLY_DEVICES env var into the
        # ShellyDevice table - only runs if that table is completely empty.
        seed_shelly_devices()
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
        # Same reloader guard as the browser-open above: debug mode re-runs
        # this whole script in a subprocess, and without the guard both the
        # watcher and the reloaded process would start their own poller.
        start_shelly_history_poller()
        start_solis_history_poller()
        start_mqtt_listener()

    app.run(debug=debug_mode)