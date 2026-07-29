"""Empty pytest root marker - its presence makes pytest add the repo root
(not testing/) to sys.path, so testing/test_security_fixes.py's `import app`
resolves regardless of where pytest is invoked from."""
