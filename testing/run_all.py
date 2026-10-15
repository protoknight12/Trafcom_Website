"""Run every testing/test_*.py, each in its own process (one app + one SQLite
database per file - they interfere when sharing a process).

    python -m testing.run_all [-j N] [--timeout SEC] [substring ...]

Exit 0 and 5 (plain-assert script: asserts run at import, nothing collected)
both count as pass. Exit code 1 if any file failed or timed out.
"""
import argparse
import glob
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(path, timeout):
    t = time.time()
    try:
        p = subprocess.run([sys.executable, '-m', 'pytest', path, '-q', '-p', 'no:cacheprovider'], cwd=ROOT,
                           capture_output=True, text=True, errors='replace', timeout=timeout)
        ok, out = p.returncode in (0, 5), p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        ok, out = False, f'TIMEOUT after {timeout}s\n' + str(e.stdout or '')
    return path, ok, time.time() - t, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-j', type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('only', nargs='*')
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(HERE, 'test_*.py')))
    if a.only:
        files = [f for f in files if any(s in os.path.basename(f) for s in a.only)]
    t0, failed = time.time(), []
    with ThreadPoolExecutor(a.j) as ex:
        for fut in as_completed([ex.submit(run, f, a.timeout) for f in files]):
            path, ok, secs, out = fut.result()
            print(f"{'ok  ' if ok else 'FAIL'} {secs:6.1f}s  {os.path.basename(path)}", flush=True)
            if not ok:
                failed.append((path, out))
    print(f'\n{len(files) - len(failed)}/{len(files)} files passed in {time.time() - t0:.0f}s')
    for path, out in failed:
        print(f'\n===== FAILED: {os.path.basename(path)} (last 40 lines) =====')
        print('\n'.join(out.splitlines()[-40:]))
    if failed:
        print('\nFailing files: ' + ', '.join(os.path.basename(p) for p, _ in failed))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
