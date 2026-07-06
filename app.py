import os
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox, path

app = Flask(__name__)
app.config['SECRET_KEY'] = 'REDACTED_ROTATED_SECRET_KEY'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'  # Resolves inside instance/ folder automatically
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB limit

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# ----------------- SECURITY DATA MODELS -----------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)  # Stores securely hashed strings
    files = db.relationship('DxfFile', backref='owner', lazy=True)


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


# ----------------- ROBUST CAD GEOMETRY PARSER -----------------

MATERIAL_CONFIG = {
    "steel": {"name": "Стомана (2mm)", "cost_per_mm2": 0.00005, "cut_cost_per_mm": 0.0015},
    "aluminum": {"name": "Алуминий (3mm)", "cost_per_mm2": 0.00008, "cut_cost_per_mm": 0.0020},
    "wood": {"name": "Шперплат (6mm)", "cost_per_mm2": 0.00002, "cut_cost_per_mm": 0.0008}
}


def analyze_dxf_geometry(file_path):
    """Extracts bounding box and total toolpath cut length with multiple fallbacks."""
    try:
        doc = ezdxf.readfile(file_path)
        msp = doc.modelspace()

        # Fallback Level 1: Fast boundary query (ignores missing text fonts/complex block dependencies)
        try:
            extents = bbox.extents(msp, fast=True)
            if extents.has_data:
                width = extents.size.x
                height = extents.size.y
            else:
                width, height = 0.0, 0.0
        except Exception:
            width, height = 0.0, 0.0

        total_length = 0.0
        # Fallback Level 2: High-level vector grouping
        try:
            all_paths = path.make_paths(msp)
            total_length = sum(p.length() for p in all_paths)
        except Exception:
            pass

        # Fallback Level 3: Individual primitive segmentation loop if high-level parser breaks
        if total_length == 0.0:
            for entity in msp:
                try:
                    dtype = entity.dxftype()
                    if dtype == 'LINE':
                        start, end = entity.dxf.start, entity.dxf.end
                        total_length += ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
                    elif dtype == 'CIRCLE':
                        total_length += 2 * 3.1415926535 * entity.dxf.radius
                    elif dtype == 'ARC':
                        r = entity.dxf.radius
                        span = entity.dxf.end_angle - entity.dxf.start_angle
                        if span < 0:
                            span += 360
                        total_length += r * (span * 3.1415926535 / 180.0)
                    elif dtype in ('LWPOLYLINE', 'POLYLINE'):
                        total_length += path.make_path(entity).length()
                except Exception:
                    continue

        # Fallback Level 4: Coordinate scanning if boundary box returns completely flat
        if width == 0 and height == 0:
            xs, ys = [], []
            for e in msp:
                try:
                    if e.dxftype() == 'LINE':
                        xs.extend([e.dxf.start[0], e.dxf.end[0]])
                        ys.extend([e.dxf.start[1], e.dxf.end[1]])
                except Exception:
                    continue
            if xs and ys:
                width, height = max(xs) - min(xs), max(ys) - min(ys)

        # Safeguard against rendering divisions if elements exist but sizes are micro-metric
        if width == 0 and height == 0 and total_length > 0:
            width, height = 10.0, 10.0

        return abs(round(width, 2)), abs(round(height, 2)), abs(round(total_length, 2))
    except Exception as e:
        print(f"Critical DXF Error: {e}")
        return None, None, None


def calculate_cnc_price(width, height, total_length, material):
    config = MATERIAL_CONFIG.get(material)
    if not config: return 0.0
    return round((width * height * config["cost_per_mm2"]) + (total_length * config["cut_cost_per_mm"]) + 5.00, 2)


# ----------------- CONTROL ROUTING -----------------

@app.route('/')
def index():
    return redirect(url_for('dashboard')) if current_user.is_authenticated else redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Невалидно потребителско име или парола.')
    return render_template('login.html')


@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()

    if not username or not password:
        flash('Моля попълнете всички полета.')
        return redirect(url_for('login'))

    if User.query.filter_by(username=username).first():
        flash('Потребителското име вече е заето.')
        return redirect(url_for('login'))

    # Salted cryptographically strong password derivation
    secure_pass = generate_password_hash(password, method='scrypt')
    new_user = User(username=username, password=secure_pass)
    db.session.add(new_user)
    db.session.commit()
    flash('Успешна регистрация! Моля, влезте в профила си.')
    return redirect(url_for('login'))


@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'dxf_file' not in request.files:
        return jsonify({'error': 'Няма качен файл'}), 400
    file = request.files['dxf_file']
    material = request.form.get('material')

    if file.filename == '' or not file.filename.lower().endswith('.dxf'):
        return jsonify({'error': 'Невалиден файлов формат. Моля качвайте само .dxf файлове.'}), 400
    if material not in MATERIAL_CONFIG:
        return jsonify({'error': 'Невалиден избор на материал.'}), 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{current_user.id}_{filename}")
    file.save(file_path)

    width, height, total_length = analyze_dxf_geometry(file_path)
    if width is None:
        return jsonify({'error': 'Грешка при извличане на геометрията от DXF файла.'}), 400

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


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)