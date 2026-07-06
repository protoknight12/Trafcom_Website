import os
import shutil
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ezdxf
from ezdxf import bbox, path

app = Flask(__name__)
app.config['SECRET_KEY'] = 'REDACTED_ROTATED_SECRET_KEY'
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql+psycopg2://postgres:REDACTED_ROTATED_PASSWORD@localhost:5432/cnc_calculator_db'
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# ----------------- МОДЕЛИ В БАЗАТА ДАННИ -----------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    # Change 150 to 255 here:
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)


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
    "steel": {"name": "Стомана (2mm)", "cost_per_mm2": 0.00005, "cut_cost_per_mm": 0.0015},
    "aluminum": {"name": "Алуминий (3mm)", "cost_per_mm2": 0.00008, "cut_cost_per_mm": 0.0020},
    "wood": {"name": "Шперплат (6mm)", "cost_per_mm2": 0.00002, "cut_cost_per_mm": 0.0008}
}


def analyze_dxf_geometry(file_path):
    try:
        doc = ezdxf.readfile(file_path)
        msp = doc.modelspace()

        try:
            extents = bbox.extents(msp, fast=True)
            if extents.has_data:
                width, height = extents.size.x, extents.size.y
            else:
                width, height = 0.0, 0.0
        except Exception:
            width, height = 0.0, 0.0

        total_length = 0.0
        try:
            all_paths = path.make_paths(msp)
            total_length = sum(p.length() for p in all_paths)
        except Exception:
            pass

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
                        if span < 0: span += 360
                        total_length += r * (span * 3.1415926535 / 180.0)
                    elif dtype in ('LWPOLYLINE', 'POLYLINE'):
                        total_length += path.make_path(entity).length()
                except Exception:
                    continue

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

        if width == 0 and height == 0 and total_length > 0:
            width, height = 10.0, 10.0

        return abs(round(width, 2)), abs(round(height, 2)), abs(round(total_length, 2))
    except Exception as e:
        print(f"DXF Parsing Error: {e}")
        return None, None, None


def calculate_cnc_price(width, height, total_length, material):
    config = MATERIAL_CONFIG.get(material)
    if not config: return 0.0
    return round((width * height * config["cost_per_mm2"]) + (total_length * config["cut_cost_per_mm"]) + 5.00, 2)


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
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))
    user_files = DxfFile.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', files=user_files, materials=MATERIAL_CONFIG)


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
    if not current_user.is_admin: return jsonify({'error': 'Неоторизиран достъп'}), 403
    user_to_delete = User.query.get_or_create = User.query.get(user_id)
    if user_to_delete:
        db.session.delete(user_to_delete)
        db.session.commit()
        flash('Потребителят и неговите файлове бяха изтрити.')
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