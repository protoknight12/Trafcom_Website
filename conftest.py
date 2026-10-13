"""Pytest root marker (its presence puts the repo root, not testing/, on
sys.path so `import app` resolves) plus the test-database safety guard.

`import testing` runs testing/__init__.py, which forces DATABASE_URL to a
throwaway SQLite file *before* any test module imports `app` (app.py would
otherwise load the real database URL from .env). Importing it here as well is
belt and braces: conftest.py is always loaded before test modules.
"""
import os
import sys

import pytest

import testing  # noqa: F401  (side effect: forces a SQLite DATABASE_URL)


def _abort_unless_sqlite(where):
    urls = [('env DATABASE_URL', os.environ.get('DATABASE_URL', ''))]
    mod = sys.modules.get('app')  # never import app here - only look if a test already did
    if mod is not None and hasattr(mod, 'app'):
        urls.append(("app.config['SQLALCHEMY_DATABASE_URI']",
                     str(mod.app.config.get('SQLALCHEMY_DATABASE_URI', ''))))
    for name, url in urls:
        if not url.startswith('sqlite'):
            pytest.exit(f'REFUSING TO RUN TESTS ({where}): {name} is not SQLite - '
                        'tests must never touch the real database.', returncode=3)


def pytest_collectstart(collector):  # before each test module is imported
    _abort_unless_sqlite('collection')


def pytest_runtest_setup(item):  # before each test (app is imported by now)
    _abort_unless_sqlite('test setup')
