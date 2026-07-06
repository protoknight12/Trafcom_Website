import os
import json
import math
import uuid
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox

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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------- DXF ГЕОМЕТРИЯ И ЦЕНИ -----------------

MATERIAL_CONFIG = {
    "wood": {"cost_per_mm2": 0.00001, "cut_cost_per_mm": 0.0008, "cost_per_pierce": 0.05, "name": "Дървесен материал / МДФ"},
    "steel": {"cost_per_mm2": 0.00002, "cut_cost_per_mm": 0.0015, "cost_per_pierce": 0.15, "name": "Въглеродна стомана"},
    "stainless_steel": {"cost_per_mm2": 0.00005, "cut_cost_per_mm": 0.0025, "cost_per_pierce": 0.25, "name": "Неръждаема стомана"},
    "aluminum": {"cost_per_mm2": 0.00004, "cut_cost_per_mm": 0.0020, "cost_per_pierce": 0.20, "name": "Алуминий"},
    "copper": {"cost_per_mm2": 0.00012, "cut_cost_per_mm": 0.0040, "cost_per_pierce": 0.40, "name": "Мед"},
    "brass": {"cost_per_mm2": 0.00009, "cut_cost_per_mm": 0.0035, "cost_per_pierce": 0.35, "name": "Месинг"},
    "galvanized": {"cost_per_mm2": 0.00003, "cut_cost_per_mm": 0.0018, "cost_per_pierce": 0.18, "name": "Поцинкована ламарина"}
}


def process_entity(entity):
    """
    Reads a single DXF entity ONCE and extracts everything the app needs from
    it: its cutting length, its endpoint segments (for pierce/loop detection),
    and a JSON-serializable shape (for the 2D viewer).

    Previously these three pieces of data were each computed via a separate
    full pass over every entity in the drawing (3x the iteration and 3x the
    ezdxf attribute-access overhead for large files). Combining them into one
    pass keeps behavior identical while roughly tripling geometry-extraction
    throughput on drawings with many entities.

    Returns a tuple: (length_contribution, segments, shape_or_None)
    """
    dtype = entity.dxftype()
    length = 0.0
    segments = []
    shape = None

    try:
        if dtype == 'LINE':
            start = (entity.dxf.start.x, entity.dxf.start.y)
            end = (entity.dxf.end.x, entity.dxf.end.y)

            length = math.dist(start, end)
            segments.append((start, end))
            shape = {'type': 'line', 'x1': start[0], 'y1': start[1], 'x2': end[0], 'y2': end[1]}

        elif dtype == 'CIRCLE':
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            top_point = (cx, cy + r)

            length = 2 * math.pi * r
            # A circle is a closed loop that touches itself - model it as a
            # single segment starting and ending at the same point.
            segments.append((top_point, top_point))
            shape = {'type': 'circle', 'cx': cx, 'cy': cy, 'r': r}

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
            shape = {'type': 'arc', 'cx': cx, 'cy': cy, 'r': r, 'start_angle': start_angle, 'end_angle': end_angle}

        elif dtype in ('LWPOLYLINE', 'POLYLINE'):
            points = [(p[0], p[1]) for p in entity.get_points(format='xy')]
            if points:
                for i in range(len(points) - 1):
                    length += math.dist(points[i], points[i + 1])
                    segments.append((points[i], points[i + 1]))

                if entity.is_closed:
                    length += math.dist(points[-1], points[0])
                    segments.append((points[-1], points[0]))

                shape = {'type': 'polyline', 'points': points, 'closed': bool(entity.is_closed)}

    except Exception:
        pass  # Ignore malformed entities safely, keep processing the rest

    return length, segments, shape


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


def analyze_dxf_geometry(file_path):
    """
    Parses a DXF file to determine outer dimensions, total cutting length,
    a precise pierce count using direct entity extraction and graph matching,
    and a list of drawable shapes for the 2D viewer.
    """
    try:
        doc = ezdxf.readfile(file_path)
        msp = doc.modelspace()

        # 1. Calculate Bounding Box Dimensions
        try:
            extents = bbox.extents(msp, fast=True)
            if extents.has_data:
                width, height = extents.size.x, extents.size.y
            else:
                width, height = 0.0, 0.0
        except Exception:
            width, height = 0.0, 0.0

        # 2. Single pass over every entity: accumulate cutting length, collect
        # endpoint segments (for pierce detection), and collect drawable shapes.
        total_length = 0.0
        all_segments = []
        shapes = []

        for entity in msp:
            entity_length, entity_segments, entity_shape = process_entity(entity)
            total_length += entity_length
            all_segments.extend(entity_segments)
            if entity_shape is not None:
                shapes.append(entity_shape)

        # 3. Graph connectivity component counting to determine pierce count
        pierce_count = count_pierces(all_segments)

        # 4. Fallbacks to prevent returning zeros for weirdly scaled files
        if width == 0 and height == 0 and total_length > 0:
            width, height = 10.0, 10.0
        if pierce_count == 0 and total_length > 0:
            pierce_count = 1

        return abs(round(width, 2)), abs(round(height, 2)), abs(round(total_length, 2)), pierce_count, shapes

    except Exception as e:
        print(f"Critical DXF Parsing Error: {e}")
        return None, None, None, None, None


def calculate_cnc_price(width, height, total_length, pierce_count, material):
    config = MATERIAL_CONFIG.get(material)
    if not config:
        return 0.0

    material_surface_cost = width * height * config["cost_per_mm2"]
    cutting_lineal_cost = total_length * config["cut_cost_per_mm"]
    piercing_total_cost = pierce_count * config["cost_per_pierce"]

    total_calculated_euro = material_surface_cost + cutting_lineal_cost + piercing_total_cost + BASE_SETUP_FEE
    return round(total_calculated_euro, 2)


# ----------------- МАРШРУТИ И ЛОГИКА -----------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


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
                if chosen_material not in MATERIAL_CONFIG:
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
    return render_template('dashboard.html', uploads=user_uploads)


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
    return render_template('admin.html', users=all_users)


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
    # Defaults to debug mode for local development. Set FLASK_DEBUG=0 in your
    # environment before deploying anywhere public - debug mode exposes an
    # interactive code-execution debugger on unhandled exceptions.
    debug_mode = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(debug=debug_mode)