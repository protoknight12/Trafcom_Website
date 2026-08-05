from app import app, start_shelly_history_poller

# Runs once per worker process on import - gunicorn/waitress import this
# module fresh in each worker, unlike app.py's db.create_all() etc. which are
# dev-only (see app.py's __main__ block). See start_shelly_history_poller()'s
# docstring for the multi-worker duplicate-logging caveat.
start_shelly_history_poller()

if __name__ == '__main__':
    app.run()
