import os
import json
import math
import uuid
import webbrowser
import threading
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox
from ezdxf.math import bulge_to_arc

# Optional: load a local .env file if python-dotenv is installed, so secrets
# can be kept out of source control. Safe no-op if the package isn't present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


app = Flask(__name__)

# ----------------- APP CONFIGURATION -----------------
# Prefer environment variables so real secrets never live in source control.
# The hardcoded values below are fallbacks so the app still runs out-of-the-box
# in local dev - replace them (via a .env file or real env vars) before
# deploying anywhere public, and rotate the DB password since it has been
# shared in plaintext during development.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-cnc-key-98765')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'postgresql+psycopg2://postgres:2205boyanB+-@localhost:5432/cnc_calculator_db'
)
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Flat fee added to every job to cover machine setup/initialization overhead.
BASE_SETUP_FEE = 5.00

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# ----------------- МОДЕЛИ В БАЗАТА ДАННИ -----------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    uploads = db.relationship('DxfFile', cascade='all, delete-orphan', backref='owner', lazy=True)


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

    material = db.relationship('MaterialPrice')


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

    product_details = db.relationship('ProductDetail', cascade='all, delete-orphan', backref='product', lazy=True)
    extra_costs = db.relationship('ProductExtraCost', cascade='all, delete-orphan', backref='product', lazy=True)


class ProductDetail(db.Model):
    """Join table: which Details compose a Product, and in what quantity."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    detail_id = db.Column(db.Integer, db.ForeignKey('detail.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    detail = db.relationship('Detail')


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
    "wood": {"cost_per_m2": 10.00, "cost_per_meter_cut": 0.80, "cost_per_pierce": 0.05, "name": "Дървесен материал / МДФ"},
    "steel": {"cost_per_m2": 20.00, "cost_per_meter_cut": 1.50, "cost_per_pierce": 0.15, "name": "Въглеродна стомана"},
    "stainless_steel": {"cost_per_m2": 50.00, "cost_per_meter_cut": 2.50, "cost_per_pierce": 0.25, "name": "Неръждаема стомана"},
    "aluminum": {"cost_per_m2": 40.00, "cost_per_meter_cut": 2.00, "cost_per_pierce": 0.20, "name": "Алуминий"},
    "copper": {"cost_per_m2": 120.00, "cost_per_meter_cut": 4.00, "cost_per_pierce": 0.40, "name": "Мед"},
    "brass": {"cost_per_m2": 90.00, "cost_per_meter_cut": 3.50, "cost_per_pierce": 0.35, "name": "Месинг"},
    "galvanized": {"cost_per_m2": 30.00, "cost_per_meter_cut": 1.80, "cost_per_pierce": 0.18, "name": "Поцинкована ламарина"}
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
            shapes.append({'type': 'arc', 'cx': cx, 'cy': cy, 'r': r, 'start_angle': start_angle, 'end_angle': end_angle})

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

        elif s['type'] == 'polyline':
            for x, y in s['points']:
                expand(x, y)

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

        return float(abs(round(width, 2))), float(abs(round(height, 2))), float(abs(round(total_length, 2))), pierce_count, shapes

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


# ----------------- МАРШРУТИ И ЛОГИКА -----------------

@app.route('/')
def index():
    # Both anonymous and logged-in visitors see the public landing page now -
    # index.html adapts its nav CTA based on current_user.is_authenticated
    # (showing "Към Таблото" instead of Login/Register). Apps like /dashboard
    # and /generator still require login via @login_required regardless.
    return render_template('index.html')


@app.route('/generator')
@login_required
def generator():
    # Requires login, same as every other app (matches the "apps require an
    # account, the public site doesn't" design used across the project).
    materials = MaterialPrice.query.order_by(MaterialPrice.display_name).all()
    return render_template('generator.html', materials=materials)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            if user.is_admin:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
        flash('Невалидно потребителско име или парола.')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if not username or not password:
            flash('Моля попълнете всички полета.')
            return redirect(url_for('register'))

        if User.query.filter_by(username=username).first():
            flash('Потребителското име вече е заето.')
            return redirect(url_for('register'))

        secure_pass = generate_password_hash(password, method='scrypt')
        new_user = User(username=username, password=secure_pass, is_admin=False)
        db.session.add(new_user)
        db.session.commit()
        flash('Успешна регистрация! Моля, влезте в профила си.')
        return redirect(url_for('login'))

    return render_template('register.html')


# Окончателно възстановен маршут за потребителското табло
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('Грешка: Няма избран файл.', 'danger')
            return redirect(request.url)

        file = request.files['file']
        if file.filename == '':
            flash('Грешка: Не сте избрали файл.', 'danger')
            return redirect(request.url)

        if file and file.filename.lower().endswith('.dxf'):
            temp_path = None
            try:
                filename = secure_filename(file.filename)
                # Save to the private upload folder (not the public static/
                # folder) with a unique prefix, so concurrent uploads never
                # collide and the raw file is never briefly web-accessible.
                temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
                file.save(temp_path)

                # Extracts geometric metrics, pierce count, and drawable shapes
                width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(temp_path)

                if width is None or total_length is None:
                    flash('Грешка при обработката на DXF структурата.', 'danger')
                    return redirect(url_for('dashboard'))

                chosen_material = request.form.get('material', 'steel')
                chosen_material_row = MaterialPrice.query.filter_by(key=chosen_material).first()
                if not chosen_material_row:
                    flash('Невалиден избор на материал.', 'danger')
                    return redirect(url_for('dashboard'))

                price = calculate_cnc_price(width, height, total_length, pierce_count, chosen_material)

                new_file_record = DxfFile(
                    filename=file.filename,
                    material=chosen_material,
                    width=width,
                    height=height,
                    total_length=total_length,
                    calculated_price=price,
                    user_id=current_user.id,
                    geometry_json=json.dumps(shapes)
                )

                db.session.add(new_file_record)
                db.session.commit()

                flash(f'Файлът "{file.filename}" беше изчислен успешно с включени пробиви ({pierce_count} бр.)!',
                      'success')
                return redirect(url_for('dashboard'))

            except Exception as e:
                db.session.rollback()
                flash(f'Критична грешка при обработка/запис: {str(e)}', 'danger')
                return redirect(url_for('dashboard'))
            finally:
                # Always clean up the temp file, regardless of success/failure.
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
        else:
            flash('Невалиден формат! Системата приема само .dxf файлове.', 'danger')
            return redirect(url_for('dashboard'))

    user_uploads = DxfFile.query.filter_by(user_id=current_user.id).order_by(DxfFile.id.desc()).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.display_name).all()
    return render_template('dashboard.html', uploads=user_uploads, materials=materials)


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


# ----------------- АДМИНИСТРАТОРСКИ МАРШРУТИ -----------------

@app.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.')
        return redirect(url_for('dashboard'))
    all_users = User.query.filter(User.id != current_user.id).all()
    materials = MaterialPrice.query.order_by(MaterialPrice.display_name).all()
    details = Detail.query.order_by(Detail.name).all()
    products = Product.query.order_by(Product.name).all()
    product_pricing = {p.id: calculate_product_pricing(p) for p in products}
    return render_template(
        'admin.html', users=all_users, materials=materials,
        details=details, products=products, product_pricing=product_pricing
    )


@app.route('/admin/create_user', methods=['POST'])
@login_required
def admin_create_user():
    if not current_user.is_admin: return jsonify({'error': 'Неоторизиран достъп'}), 403
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()

    if not username or not password:
        flash('Попълнете всички полета.')
        return redirect(url_for('admin_dashboard'))

    if User.query.filter_by(username=username).first():
        flash('Потребителското име вече съществува.')
        return redirect(url_for('admin_dashboard'))

    grant_admin = request.form.get('is_admin') == 'true'
    secure_pass = generate_password_hash(password, method='scrypt')
    new_user = User(username=username, password=secure_pass, is_admin=grant_admin)
    db.session.add(new_user)
    db.session.commit()
    flash(f'Успешно създаден потребител: {username}')
    return redirect(url_for('admin_dashboard'))


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
    except ValueError:
        flash('Всички цени трябва да бъдат валидни числа.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if cost_per_m2 < 0 or cost_per_meter_cut < 0 or cost_per_pierce < 0:
        flash('Цените не могат да бъдат отрицателни числа.', 'danger')
        return redirect(url_for('admin_dashboard'))

    # Round to 2 decimals - keeps prices in a simple, everyday currency
    # format rather than accumulating long float tails over repeated edits.
    material.cost_per_m2 = round(cost_per_m2, 2)
    material.cost_per_meter_cut = round(cost_per_meter_cut, 2)
    material.cost_per_pierce = round(cost_per_pierce, 2)
    db.session.commit()

    flash(f'Цените за "{material.display_name}" бяха обновени успешно.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/add_material', methods=['POST'])
@login_required
def admin_add_material():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    display_name = request.form.get('display_name', '').strip()
    if not display_name:
        flash('Моля въведете име на материала.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if MaterialPrice.query.filter_by(display_name=display_name).first():
        # Lets your boss add e.g. "Алуминий 2мм" and "Алуминий 10мм" as
        # distinct priced entries, while still catching accidental exact
        # duplicates of the same name.
        flash(f'Вече съществува материал с име "{display_name}".', 'danger')
        return redirect(url_for('admin_dashboard'))

    try:
        cost_per_m2 = float(request.form.get('cost_per_m2', ''))
        cost_per_meter_cut = float(request.form.get('cost_per_meter_cut', ''))
        cost_per_pierce = float(request.form.get('cost_per_pierce', ''))
    except ValueError:
        flash('Всички цени трябва да бъдат валидни числа.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if cost_per_m2 < 0 or cost_per_meter_cut < 0 or cost_per_pierce < 0:
        flash('Цените не могат да бъдат отрицателни числа.', 'danger')
        return redirect(url_for('admin_dashboard'))

    # The key is just an opaque internal identifier (used in DxfFile.material
    # and the dashboard <select> value) - it's never shown to users, so a
    # simple auto-generated id-based key avoids any need to transliterate
    # Cyrillic display names into a URL-safe slug.
    new_material = MaterialPrice(
        key='pending',  # placeholder, replaced with a real unique key below
        display_name=display_name,
        cost_per_m2=round(cost_per_m2, 2),
        cost_per_meter_cut=round(cost_per_meter_cut, 2),
        cost_per_pierce=round(cost_per_pierce, 2)
    )
    db.session.add(new_material)
    db.session.flush()  # assigns new_material.id without a full commit yet
    new_material.key = f'material_{new_material.id}'
    db.session.commit()

    flash(f'Материалът "{display_name}" беше добавен успешно.', 'success')
    return redirect(url_for('admin_dashboard'))


# ----------------- БИБЛИОТЕКА С ДЕТАЙЛИ (Detail catalog) -----------------

@app.route('/admin/details/add', methods=['POST'])
@login_required
def admin_add_detail():
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    name = request.form.get('name', '').strip()
    material_key = request.form.get('material', '')

    if not name:
        flash('Моля въведете име на детайла.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if not MaterialPrice.query.filter_by(key=material_key).first():
        flash('Невалиден избор на материал.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if 'file' not in request.files or request.files['file'].filename == '':
        flash('Моля качете .dxf файл за детайла.', 'danger')
        return redirect(url_for('admin_dashboard'))

    file = request.files['file']
    if not file.filename.lower().endswith('.dxf'):
        flash('Невалиден формат! Приемат се само .dxf файлове.', 'danger')
        return redirect(url_for('admin_dashboard'))

    temp_path = None
    try:
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
        file.save(temp_path)

        width, height, total_length, pierce_count, shapes = analyze_dxf_geometry(temp_path)
        if width is None:
            flash('Грешка при обработката на DXF структурата.', 'danger')
            return redirect(url_for('admin_dashboard'))

        price = calculate_cnc_price(width, height, total_length, pierce_count, material_key)

        new_detail = Detail(
            name=name, material_key=material_key, width=width, height=height,
            total_length=total_length, pierce_count=pierce_count,
            calculated_price=price, geometry_json=json.dumps(shapes)
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

    return redirect(url_for('admin_dashboard'))


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
        return redirect(url_for('admin_dashboard'))

    db.session.delete(detail)
    db.session.commit()
    flash(f'Детайлът "{detail.name}" беше изтрит.', 'success')
    return redirect(url_for('admin_dashboard'))


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
        return redirect(url_for('admin_dashboard'))

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
    pricing = calculate_product_pricing(product)
    return render_template('product_edit.html', product=product, all_details=all_details, pricing=pricing)


@app.route('/admin/products/<int:product_id>/update', methods=['POST'])
@login_required
def admin_product_update(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Моля въведете име на продукта.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    try:
        markup_percent = float(request.form.get('markup_percent', '0') or 0)
    except ValueError:
        flash('Надценката трябва да бъде валидно число.', 'danger')
        return redirect(url_for('admin_product_edit', product_id=product.id))

    product.name = name
    product.description = request.form.get('description', '').strip()
    product.markup_percent = round(markup_percent, 2)
    db.session.commit()

    flash('Продуктът беше обновен успешно.', 'success')
    return redirect(url_for('admin_product_edit', product_id=product.id))


@app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
@login_required
def admin_product_delete(product_id):
    if not current_user.is_admin:
        flash('Нямате достъп до тази страница.', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)
    name = product.name
    db.session.delete(product)  # cascades to ProductDetail/ProductExtraCost rows
    db.session.commit()
    flash(f'Продуктът "{name}" беше изтрит.', 'success')
    return redirect(url_for('admin_dashboard'))


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
    return render_template('offer.html', product=product, pricing=pricing, customer_name=customer_name)


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
        return redirect(url_for('admin_dashboard'))

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

    return redirect(url_for('admin_dashboard'))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))



if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Автоматично генериране на СИСТЕМЕН АДМИН при липса на такъв
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            db.session.add(User(
                username='admin',
                password=generate_password_hash('admin123', method='scrypt'),
                is_admin=True
            ))
            db.session.commit()
        # Populate the MaterialPrice table with defaults on first run only -
        # existing rows (including any admin-edited prices) are never touched.
        seed_material_prices()
    # Defaults to debug mode for local development. Set FLASK_DEBUG=0 in your
    # environment before deploying anywhere public - debug mode exposes an
    # interactive code-execution debugger on unhandled exceptions.
    debug_mode = os.environ.get('FLASK_DEBUG', '1') == '1'

    # Auto-open the app in the browser on startup. When debug_mode is on,
    # Flask's reloader re-runs this entire script in a subprocess - without
    # this guard the browser would pop open twice. WERKZEUG_RUN_MAIN is only
    # set to 'true' inside that reloaded subprocess (the one actually
    # serving requests), so we only open there; when debug is off, there's
    # no reloader/subprocess at all, so we open immediately instead.
    if not debug_mode or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Timer(1.0, lambda: webbrowser.open('http://127.0.0.1:5000/')).start()

    app.run(debug=debug_mode)