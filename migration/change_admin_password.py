"""One-off: sets (or creates) the 'admin' user's password. Replaces the old
auto-seeded admin/admin123 default (removed from app.py - see the
if __name__ == '__main__': block) which was a real risk sitting in public
source. Run this against your real DATABASE_URL - it never hardcodes a
password in source, it only reads one from the command line.

    python -m migration.change_admin_password <new-password>
"""
import sys

from app import app, db, User, generate_password_hash

if len(sys.argv) != 2:
    raise SystemExit("Usage: python -m migration.change_admin_password <new-password>")

new_password = sys.argv[1]
if len(new_password) < 8:
    raise SystemExit("Password must be at least 8 characters.")

with app.app_context():
    admin = User.query.filter_by(username='admin').first()
    if admin:
        admin.password = generate_password_hash(new_password, method='scrypt')
        db.session.commit()
        print("Updated password for existing 'admin' user.")
    else:
        db.session.add(User(
            username='admin',
            password=generate_password_hash(new_password, method='scrypt'),
            role='admin'
        ))
        db.session.commit()
        print("Created 'admin' user with the given password.")
