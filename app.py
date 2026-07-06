import os
import shutil
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox, path
import math
app = Flask(__name__)
app.config['SECRET_KEY'] = 'REDACTED_ROTATED_SECRET_KEY'
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql+psycopg2://postgres:REDACTED_ROTATED_PASSWORD@localhost:5432/cnc_calculator_db'
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

CNC_PRICE_PER_MM = 0.03
CNC_PRICE_PER_PIERCE = 0.10
CNC_BASE_SETUP_FEE = 15.00
CNC_WASTE_MULTIPLIER = 1.15

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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------- DXF ГЕОМЕТРИЯ И ЦЕНИ -----------------

MATERIAL_CONFIG = {
    "wood": {"cost_per_mm2": 0.00001, "cut_cost_per_mm": 0.0008, "cost_per_pierce": 0.05},
    "steel": {"cost_per_mm2": 0.00002, "cut_cost_per_mm": 0.0015, "cost_per_pierce": 0.15},
    "stainless_steel": {"cost_per_mm2": 0.00005, "cut_cost_per_mm": 0.0025, "cost_per_pierce": 0.25},
    "aluminum": {"cost_per_mm2": 0.00004, "cut_cost_per_mm": 0.0020, "cost_per_pierce": 0.20},
    "copper": {"cost_per_mm2": 0.00012, "cut_cost_per_mm": 0.0040, "cost_per_pierce": 0.40},
    "brass": {"cost_per_mm2": 0.00009, "cut_cost_per_mm": 0.0035, "cost_per_pierce": 0.35},
    "galvanized": {"cost_per_mm2": 0.00003, "cut_cost_per_mm": 0.0018, "cost_per_pierce": 0.18}
}


def get_entity_endpoints(entity):
    """
    Extracts the physical start and end (X, Y) points of raw DXF geometry entities.
    Returns a list of segment tuples: [((x1, y1), (x2, y2)), ...]
    """
    dtype = entity.dxftype()
    segments = []

    try:
        if dtype == 'LINE':
            s = (entity.dxf.start.x, entity.dxf.start.y)
            e = (entity.dxf.end.x, entity.dxf.end.y)
            segments.append((s, e))

        elif dtype == 'CIRCLE':
            # A circle is a closed loop that touches itself.
            # We can model it as a single segment starting and ending at the same top point.
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            p = (cx, cy + r)
            segments.append((p, p))

        elif dtype == 'ARC':
            # Calculate actual start/end coordinates of the arc using trigonometry
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            sa = math.radians(entity.dxf.start_angle)
            ea = math.radians(entity.dxf.end_angle)

            s = (cx + r * math.cos(sa), cy + r * math.sin(sa))
            e = (cx + r * math.cos(ea), cy + r * math.sin(ea))
            segments.append((s, e))

        elif dtype in ('LWPOLYLINE', 'POLYLINE'):
            # Explode polyline points into individual consecutive line segments
            points = [(p[0], p[1]) for p in entity.get_points(format='xy')]
            if not points:
                return segments

            for i in range(len(points) - 1):
                segments.append((points[i], points[i + 1]))

            # If the polyline is explicitly flagged as closed, bridge back to the start
            if entity.is_closed:
                segments.append((points[-1], points[0]))

    except Exception:
        pass  # Ignore malformed entities safely

    return segments


def analyze_dxf_geometry(file_path):
    """
    Parses a DXF file to determine outer dimensions, total cutting length,
    and a precise pierce count using direct entity extraction and graph matching.
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

        # 2. Extract every single raw line segment from the CAD entities
        all_segments = []
        total_length = 0.0

        for entity in msp:
            # Accumulate lengths using raw definitions to keep length tracking alive
            try:
                dtype = entity.dxftype()
                if dtype == 'LINE':
                    total_length += math.dist(entity.dxf.start, entity.dxf.end)
                elif dtype == 'CIRCLE':
                    total_length += 2 * math.pi * entity.dxf.radius
                elif dtype == 'ARC':
                    r = entity.dxf.radius
                    span = entity.dxf.end_angle - entity.dxf.start_angle
                    if span < 0: span += 360
                    total_length += r * math.radians(span)
                elif dtype in ('LWPOLYLINE', 'POLYLINE'):
                    pts = entity.get_points(format='xy')
                    for i in range(len(pts) - 1):
                        total_length += math.dist(pts[i], pts[i + 1])
                    if entity.is_closed and pts:
                        total_length += math.dist(pts[-1], pts[0])
            except Exception:
                pass

            # Extract geometric points for pierce grouping
            all_segments.extend(get_entity_endpoints(entity))

        # 3. Graph connectivity component counting (Undirected check)
        num_segs = len(all_segments)
        pierce_count = 0

        if num_segs > 0:
            tolerance = 0.5  # Max gap distance in mm allowed between vertices
            adj = {i: [] for i in range(num_segs)}

            # Map out which fragments touch at their boundaries
            for i in range(num_segs):
                s1, e1 = all_segments[i]
                for j in range(i + 1, num_segs):
                    s2, e2 = all_segments[j]

                    # Check all 4 endpoint possibilities to capture undirected connections
                    if (math.dist(s1, s2) <= tolerance or
                            math.dist(s1, e2) <= tolerance or
                            math.dist(e1, s2) <= tolerance or
                            math.dist(e1, e2) <= tolerance):
                        adj[i].append(j)
                        adj[j].append(i)

            # Standard Breadth-First Search (BFS) to identify connected loops
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

        # 4. Fallbacks to prevent returning zeros for weirdly scaled files
        if width == 0 and height == 0 and total_length > 0:
            width, height = 10.0, 10.0
        if pierce_count == 0 and total_length > 0:
            pierce_count = 1

        return abs(round(width, 2)), abs(round(height, 2)), abs(round(total_length, 2)), pierce_count

    except Exception as e:
        print(f"Critical DXF Parsing Error: {e}")
        return None, None, None, None


def calculate_cnc_price(width, height, total_length, pierce_count, material):
    config = MATERIAL_CONFIG.get(material)
    if not config:
        return 0.0

    material_surface_cost = width * height * config["cost_per_mm2"]
    cutting_lineal_cost = total_length * config["cut_cost_per_mm"]
    piercing_total_cost = pierce_count * config["cost_per_pierce"]
    base_setup_fee = 5.00  # Flat initialization machine setup overhead

    total_calculated_euro = material_surface_cost + cutting_lineal_cost + piercing_total_cost + base_setup_fee
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

        if file and file.filename.endswith('.dxf'):
            try:
                filename = secure_filename(file.filename)
                temp_path = os.path.join('static', filename)
                file.save(temp_path)

                # Extracts geometric metrics along with total path pierces
                width, height, total_length, pierce_count = analyze_dxf_geometry(temp_path)

                if os.path.exists(temp_path):
                    os.remove(temp_path)

                if width is None or total_length is None:
                    flash('Грешка при обработката на DXF структурата.', 'danger')
                    return redirect(url_for('dashboard'))

                chosen_material = request.form.get('material', 'steel')

                # Calculate final cost incorporating the newly tracked pierce_count metric
                price = calculate_cnc_price(width, height, total_length, pierce_count, chosen_material)

                # Persists matching your existing SQL model signature perfectly
                new_file_record = DxfFile(
                    filename=file.filename,
                    material=chosen_material,
                    width=width,
                    height=height,
                    total_length=total_length,
                    calculated_price=price,
                    user_id=current_user.id
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
        else:
            flash('Невалиден формат! Системата приема само .dxf файлове.', 'danger')
            return redirect(url_for('dashboard'))

    user_uploads = DxfFile.query.filter_by(user_id=current_user.id).order_by(DxfFile.id.desc()).all()
    return render_template('dashboard.html', uploads=user_uploads)


@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'dxf_file' not in request.files:
        return jsonify({'error': 'Няма качен файл'}), 400
    file = request.files['dxf_file']
    material = request.form.get('material')

    if file.filename == '' or not file.filename.lower().endswith('.dxf'):
        return jsonify({'error': 'Невалиден файлов формат. Качвайте само .dxf.'}), 400
    if material not in MATERIAL_CONFIG:
        return jsonify({'error': 'Невалиден избор на материал.'}), 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{current_user.id}_{filename}")
    file.save(file_path)

    width, height, total_length = analyze_dxf_geometry(file_path)
    if width is None:
        return jsonify({'error': 'Грешка при извличане на геометрия от DXF.'}), 400

    price = calculate_cnc_price(width, height, total_length, material)

    new_dxf = DxfFile(
        filename=filename, material=material, width=width, height=height,
        total_length=total_length, calculated_price=price, user_id=current_user.id
    )
    db.session.add(new_dxf)
    db.session.commit()

    return jsonify({
        'filename': filename, 'width': width, 'height': height,
        'total_length': total_length, 'price': price,
        'material_name': MATERIAL_CONFIG[material]['name']
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

    secure_pass = generate_password_hash(password, method='scrypt')
    new_user = User(username=username, password=secure_pass, is_admin=False)
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

    try:
        # 1. (Your existing code that removes files from the disk goes here)
        for upload in user_to_delete.uploads:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'],f"{upload.user_id}{upload.filename}")
            if os.path.exists(file_path):
                os.remove(upload.file_path)

        # 2. Complete the database deletion
        db.session.delete(user_to_delete)
        db.session.commit()

        # Success alert banner configuration
        flash(f'Потребителят {user_to_delete.username} и неговите чертежи бяха изтрити!', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Грешка при изтриване на данни: {str(e)}', 'danger')

    # Crucial change: Redirect right back to the dashboard table view
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
    app.run(debug=True)
