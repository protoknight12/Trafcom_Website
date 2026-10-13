from app import app, start_shelly_history_poller, start_solis_history_poller, start_mqtt_listener, start_detection_worker, start_heatpump_poller

# Runs once per worker process on import - gunicorn/waitress import this
# module fresh in each worker, unlike app.py's db.create_all() etc. which are
# dev-only (see app.py's __main__ block). See start_shelly_history_poller()'s
# docstring for the multi-worker duplicate-logging caveat - same applies to
# the other two. All three are no-ops if their config isn't set (Solis/MQTT
# hosts unset), so this is safe even where they're not in use.
# Each on its own: a poller that cannot even start (bad MQTT_BROKER_PORT, a broken detector model ...) must not keep the whole
# site - order entry, login - from booting. GET /health shows which pollers are alive afterwards.
for _start in (start_shelly_history_poller, start_solis_history_poller, start_mqtt_listener, start_detection_worker,
               start_heatpump_poller):
    try:
        _start()
    except Exception:
        app.logger.exception('%s failed - the site starts without it', _start.__name__)

if __name__ == '__main__':
    app.run()
