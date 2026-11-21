// Keep the place on the page (owner's request 2026-11-21): after a reload (the frame dialog's save, the
// production report's location.reload()) or a form POST that redirects back to the same page, the page opens
// where you were instead of at the top. Saved on pagehide per path+query in sessionStorage: the anchor = the
// nearest section/row with an id at the top of the screen (an order card), plus its offset; scrollY as fallback.
// Restored only when the new page is the same path and we came from it (reload, or referrer = this page).
(function () {
    if (window.location.hash) return;     // works inside a frame dialog too (dossier, edit pages)
    var KEY = 'keep-scroll:' + window.location.pathname + window.location.search;
    var ANCHORS = 'section[id], article[id], tr[id], li[id], .admin-card[id], [data-keep-scroll][id]';

    window.addEventListener('pagehide', function () {
        var el = document.elementFromPoint(window.innerWidth / 2, 90), a = el && el.closest(ANCHORS);
        var data = { y: window.scrollY, t: Date.now(),
                     id: a ? a.id : null, off: a ? a.getBoundingClientRect().top : 0 };
        try { sessionStorage.setItem(KEY, JSON.stringify(data)); } catch (e) { /* storage blocked */ }
    });

    var nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {};
    var fromHere = false;
    try { fromHere = !!document.referrer && new URL(document.referrer).pathname === window.location.pathname; } catch (e) {}
    if (nav.type !== 'reload' && !fromHere) return;
    var saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) {}
    if (!saved || Date.now() - saved.t > 30 * 60 * 1000) return;
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

    function restore() {
        var a = saved.id && document.getElementById(saved.id);
        if (a) window.scrollTo(0, window.scrollY + a.getBoundingClientRect().top - saved.off);
        else window.scrollTo(0, saved.y);
    }
    // after the page's own scripts (filters, tree tables) have laid it out
    window.addEventListener('load', function () { restore(); requestAnimationFrame(restore); });
})();
