"""Self-check for the /login rate limit (see limiter.limit(...) in app.py).
Floods GET /login past its per-minute cap and confirms a 429 shows up -
catches a missing/broken decorator without needing a live Postgres
connection (GET /login for an anonymous session never touches the DB).

    python test_rate_limiting.py
"""
from app import app

LOGIN_LIMIT = 10  # keep in sync with @limiter.limit("10 per minute") on login()

client = app.test_client()
statuses = [client.get('/login').status_code for _ in range(LOGIN_LIMIT + 5)]

assert 200 in statuses, "expected some requests to succeed before the limit kicks in"
assert 429 in statuses, "expected the rate limit to eventually return 429 Too Many Requests"
assert statuses.index(429) >= LOGIN_LIMIT, "limit triggered earlier than configured"

print("ok")
