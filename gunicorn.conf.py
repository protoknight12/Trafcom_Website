# Default sync-worker timeout is 30s. /admin/power/history fetches a day's
# worth of meter data at a time (~25s per day-chunk, see shelly_history() in
# app.py) - right at that edge, so gunicorn kills the worker mid-request and
# the client gets gunicorn's own HTML error page instead of the route's JSON.
timeout = 120
