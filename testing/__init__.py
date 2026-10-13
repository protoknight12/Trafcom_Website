"""Safety guard: this package is imported before any test module (both for
`python -m testing.test_x` and for `pytest testing/...`), i.e. before `app` is.

app.py reads DATABASE_URL from the environment and auto-loads .env, so a test
that imports `app` without choosing its own database would run against the real
shop database (some tests delete rows at import time). Force a throwaway SQLite
file here - unconditionally, so a real URL already in the environment loses.
Tests that set their own sqlite URL before importing app still override this.

Do NOT import `app` in this file.
"""
import os
import tempfile

os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(prefix='trafcom_test_'), 'guard.db').replace('\\', '/')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
