// startPoll(fn, ms): runs fn() now and every ms. fn should return a promise (or nothing).
// A tick is skipped while the previous fn() is still pending or the tab is hidden;
// when the tab becomes visible again it refreshes at once. Returns {setMs(n)}.
function startPoll(fn, ms) {
    var busy = false, timer = null;
    function tick() {
        if (busy || document.hidden) return;
        busy = true;
        var done = function () { busy = false; };
        try { Promise.resolve(fn()).then(done, done); } catch (e) { done(); }
    }
    function arm() { clearInterval(timer); timer = setInterval(tick, ms); }
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); arm(); } });
    arm(); tick();
    return { setMs: function (n) { ms = n; arm(); } };
}
