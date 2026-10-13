"""
pytest: the runner in migration/run_once.py - every migrate_*.py runs exactly once per database (ledger table applied_once),
pending ones in name order first, then the ONCE data steps; a failing script is not recorded and stops the run; a name that is in
both groups runs once. Uses tiny fake migration modules in a temp package (the real ones need PostgreSQL).

    python -m pytest testing/test_run_once_migrations.py -q -p no:cacheprovider
"""
import importlib
import sys
import types

import pytest
from sqlalchemy import text

from app import app, db
from migration import run_once

BODY = "import fakemig_ran\nfakemig_ran.ran.append(%r)\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    pkg = tmp_path / 'fakemig'
    pkg.mkdir()
    (pkg / '__init__.py').write_text('')
    ran = []
    mod = types.ModuleType('fakemig_ran')
    mod.ran = ran
    monkeypatch.setitem(sys.modules, 'fakemig_ran', mod)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop('fakemig', None)   # a package cached by an earlier test points at that test's old temp dir
    monkeypatch.setattr(run_once, 'PACKAGE', 'fakemig')
    monkeypatch.setattr(run_once, 'DIRECTORY', str(pkg))
    monkeypatch.setattr(run_once, 'ONCE', ('data_b', 'migrate_c'))

    def add(name, body=None):
        (pkg / f'{name}.py').write_text(body or BODY % name)
        importlib.invalidate_caches()   # files created after the first import are otherwise not seen

    for n in ('migrate_b', 'migrate_a', 'migrate_c', 'data_b'):   # written out of order on purpose
        add(n)
    with app.app_context():
        db.session.execute(text("DROP TABLE IF EXISTS applied_once"))
        db.session.commit()
    yield types.SimpleNamespace(add=add, ran=ran)
    for m in [m for m in sys.modules if m == 'fakemig' or m.startswith('fakemig.')]:
        del sys.modules[m]


def ledger():
    with app.app_context():
        return {r[0] for r in db.session.execute(text("SELECT name FROM applied_once"))}


def test_runs_pending_in_order_then_once_steps_and_records(fake):
    assert run_once.main([]) == 0
    # migrate_a, migrate_b by name; then ONCE in its own order (migrate_c is listed there, so not in the first group)
    assert fake.ran == ['migrate_a', 'migrate_b', 'data_b', 'migrate_c']
    assert ledger() == {'migrate_a', 'migrate_b', 'data_b', 'migrate_c'}


def test_second_run_runs_nothing(fake):
    run_once.main([])
    fake.ran.clear()
    run_once.main([])
    assert fake.ran == []


def test_new_script_runs_alone_next_time(fake):
    run_once.main([])
    fake.ran.clear()
    fake.add('migrate_d')
    run_once.main([])
    assert fake.ran == ['migrate_d']


def test_failure_is_not_recorded_and_stops_the_run(fake):
    fake.add('migrate_b', "raise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError):
        run_once.main([])
    assert fake.ran == ['migrate_a']
    assert ledger() == {'migrate_a'}
    fake.add('migrate_b')              # fixed -> picked up again, the done one is not repeated
    fake.ran.clear()
    run_once.main([])
    assert fake.ran == ['migrate_b', 'data_b', 'migrate_c']


def test_sys_exit_zero_counts_as_success_nonzero_as_failure(fake):
    fake.add('migrate_a', "import sys\nsys.exit(0)\n")
    fake.add('migrate_b', "import sys\nsys.exit(3)\n")
    with pytest.raises(SystemExit) as e:
        run_once.main([])
    assert e.value.code == 3
    assert ledger() == {'migrate_a'}


def test_list_shows_both_groups_and_runs_nothing(fake, capsys):
    run_once.main([])
    fake.add('migrate_z')
    fake.ran.clear()
    capsys.readouterr()
    run_once.main(['--list'])
    out = capsys.readouterr().out
    assert fake.ran == []
    assert 'pending  migrate_z' in out and 'done     migrate_a' in out and 'done     data_b' in out
    assert out.count('migrate_c') == 1
