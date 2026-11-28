// Drag-to-zoom for the hand-drawn time charts (plain <canvas>, no library). Loaded by partials/navbar.html.
//   var z = ChartZoom.attach(canvas, {
//       map: function () { return {t0: .., t1: .., x0: .., x1: ..} or null },   // the time at the plot's left / right edge and
//                                                                               // those edges in CSS px from the canvas' left
//       onZoom: function (range) { ... }                                        // [t0, t1] after a drag, null = back to the whole period
//   });
//   z.set(false) - drop the zoom mark without calling onZoom (the page changed its period); z.set(true) after the page
//   re-created the canvas while zoomed (re-attach returns a fresh handle); z.zoomed() -> bool.
// Press and drag sideways draws a translucent band; on release the page redraws [t0, t1]. Double click or the
// "Целият период" button (shown while zoomed) goes back. Pointer events, so touch works too (touch-action: pan-y keeps
// vertical page scrolling). A click / hover that does not drag is left alone; the click right after a drag is swallowed.
var ChartZoom = (function () {
    var MIN_PX = 8, dragging = 0;     // dragging: a drag is in progress somewhere - busy() for pages that rebuild their charts on a timer
    var st = document.createElement('style');
    st.textContent = '.cz-band{position:absolute;pointer-events:none;background:rgba(63,167,255,.18);border-left:1px solid rgba(63,167,255,.7);' +
        'border-right:1px solid rgba(63,167,255,.7);display:none;z-index:4}' +
        '.cz-reset{position:absolute;top:4px;right:4px;z-index:6;display:none;padding:2px 8px;font-size:.75rem;cursor:pointer;' +
        'background:var(--bg-input,#222);color:var(--text-main,#eee);border:1px solid var(--border-color,#555);border-radius:4px}';
    document.head.appendChild(st);

    function attach(cv, opt) {
        if (!cv || !cv.parentNode) return {set: function () {}, zoomed: function () { return false; }};
        if (cv._chartZoom) { cv._chartZoom.opt = opt; return cv._chartZoom; }
        var box = cv.parentNode;
        if (getComputedStyle(box).position === 'static') box.style.position = 'relative';
        var band = document.createElement('div'), btn = document.createElement('button');
        band.className = 'cz-band'; btn.className = 'cz-reset'; btn.type = 'button'; btn.textContent = 'Целият период';
        btn.title = 'Връщане към целия период (или двоен клик върху графиката)';
        box.appendChild(band); box.appendChild(btn);
        cv.style.touchAction = 'pan-y';
        var h = {opt: opt, on: false}, drag = null, swallow = false;

        function px(e) { return e.clientX - cv.getBoundingClientRect().left; }
        function show(on) {           // the button sits in the canvas' top-right corner, whatever else the box holds
            h.on = on; btn.style.display = on ? 'block' : 'none';
            if (!on) return;
            var r = cv.getBoundingClientRect(), b = box.getBoundingClientRect();
            btn.style.top = (r.top - b.top + 4) + 'px'; btn.style.right = (b.right - r.right + 4) + 'px';
        }
        h.set = show;
        h.zoomed = function () { return h.on; };
        function back() { if (h.on) { show(false); h.opt.onZoom(null); } }

        cv.addEventListener('pointerdown', function (e) {
            if (e.button !== 0) return;
            var m = h.opt.map();
            if (!m || !(m.t1 > m.t0)) return;
            var x = px(e);
            if (x < m.x0 || x > m.x1) return;
            drag = {x: x, m: m, id: e.pointerId, moved: false};
        });
        cv.addEventListener('pointermove', function (e) {
            if (!drag || e.pointerId !== drag.id) return;
            var x = Math.min(drag.m.x1, Math.max(drag.m.x0, px(e)));
            if (!drag.moved && Math.abs(x - drag.x) < 4) return;
            if (!drag.moved) { drag.moved = true; dragging++; try { cv.setPointerCapture(e.pointerId); } catch (_) {}
                var sel = window.getSelection && window.getSelection(); if (sel) sel.removeAllRanges(); }
            var r = cv.getBoundingClientRect(), b = box.getBoundingClientRect();
            band.style.left = (r.left - b.left + Math.min(x, drag.x)) + 'px';
            band.style.width = Math.abs(x - drag.x) + 'px';
            band.style.top = (r.top - b.top) + 'px'; band.style.height = r.height + 'px';
            band.style.display = 'block';
            drag.x2 = x;
        });
        function end(e, cancel) {
            if (!drag || e.pointerId !== drag.id) return;
            var d = drag; drag = null; band.style.display = 'none';
            if (!d.moved) return;
            dragging = Math.max(0, dragging - 1);
            swallow = true; setTimeout(function () { swallow = false; }, 0);
            if (cancel || Math.abs(d.x2 - d.x) < MIN_PX) return;
            var m = d.m, T = function (x) { return m.t0 + (x - m.x0) / (m.x1 - m.x0) * (m.t1 - m.t0); };
            show(true);
            h.opt.onZoom([T(Math.min(d.x, d.x2)), T(Math.max(d.x, d.x2))]);
        }
        cv.addEventListener('pointerup', function (e) { end(e, false); });
        cv.addEventListener('pointercancel', function (e) { end(e, true); });
        cv.addEventListener('click', function (e) { if (swallow) { e.stopPropagation(); e.preventDefault(); } }, true);
        cv.addEventListener('dblclick', function (e) { if (h.on) { e.preventDefault(); back(); } });
        btn.addEventListener('click', function (e) { e.stopPropagation(); back(); });
        cv._chartZoom = h;
        return h;
    }

    // The points of [[t, ...], ...] with t inside r = [t0, t1] (r null = all).
    function clip(pts, r) {
        if (!r || !pts) return pts;
        var out = [];
        for (var i = 0; i < pts.length; i++) {
            var t = pts[i][0];
            if (t >= r[0] && t <= r[1]) out.push(pts[i]);
        }
        return out;
    }

    return {attach: attach, clip: clip, busy: function () { return dragging > 0; }};
})();
