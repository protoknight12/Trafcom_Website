/* Profile editor for press-brake tools (punch / die). World units = mm, y up, tip (punch) / top-centre (die) at the origin.
   A vertex is [x, y, r, a]: r = fillet radius at the corner (0 = sharp), a = radius of the arc that replaces the edge to the NEXT vertex
   (0 = straight; > 0 bulges left of travel, into the material = concave; < 0 bulges out). The server turns vertices into the real
   outline and reports every radius - the radius maths lives in one place (bending/profile.py).
   Data in: window.BEND_PROFILE = { kind, points, saveUrl, dxfUrl, outlineUrl, backUrl, clamp, diag }. */
(function () {
    const cfg = window.BEND_PROFILE;
    const NS = 'http://www.w3.org/2000/svg';
    const $ = id => document.getElementById(id);
    const svg = $('pe-canvas');
    const SQ2 = Math.SQRT2, GRID = 5, CELLS = 12;

    let pts = cfg.points.map(p => [p[0], p[1], p[2] || 0, p[3] || 0]);
    let dense = pts.map(p => [p[0], p[1]]);   // last outline the server computed (fillets and arcs expanded)
    let sel = -1, snap = 0.5, dirty = false;
    const undoStack = [], redoStack = [];
    let view = { cx: 0, cy: 0, w: 100 };

    const el = (name, attrs, parent) => {
        const e = document.createElementNS(NS, name);
        for (const k in attrs) e.setAttribute(k, attrs[k]);
        if (parent) parent.appendChild(e);
        return e;
    };
    const round = v => Math.round(v * 100) / 100;
    const snapv = v => snap > 0 ? Math.round(v / snap) * snap : v;
    const num = v => (Math.round(v * 1000) / 1000);

    // ---- history ----------------------------------------------------------------------------------------
    function pushUndo() {
        undoStack.push(JSON.stringify(pts));
        if (undoStack.length > 100) undoStack.shift();
        redoStack.length = 0;
        dirty = true;
    }
    function restore(json) { pts = JSON.parse(json); sel = -1; changed(); }
    function undo() { if (undoStack.length) { redoStack.push(JSON.stringify(pts)); restore(undoStack.pop()); } }
    function redo() { if (redoStack.length) { undoStack.push(JSON.stringify(pts)); restore(redoStack.pop()); } }

    // ---- server: outline + radii -------------------------------------------------------------------------
    let busy = false, again = false;
    async function refresh() {
        if (busy) { again = true; return; }
        busy = true;
        try {
            const r = await fetch(cfg.outlineUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN }, body: JSON.stringify({ points: pts }) });
            const d = await r.json();
            if (r.ok) { dense = d.outline; showInfo(d); toast(''); } else { toast(d.message || 'Невалиден контур.', true); }
            render();
        } catch (e) { toast('Няма връзка със сървъра.', true); }
        busy = false;
        if (again) { again = false; refresh(); }
    }
    function changed() { render(); refresh(); }

    // ---- view helpers ------------------------------------------------------------------------------------
    function viewBox() {
        const r = svg.getBoundingClientRect();
        const h = view.w * (r.height / r.width || 0.75);
        return { x: view.cx - view.w / 2, y: -(view.cy + h / 2), w: view.w, h };
    }
    function toWorld(ev) {
        const r = svg.getBoundingClientRect(), vb = viewBox();
        return [vb.x + (ev.clientX - r.left) / r.width * vb.w, -(vb.y + (ev.clientY - r.top) / r.height * vb.h)];
    }
    function fit() {
        const all = dense.concat(pts.map(p => [p[0], p[1]]));
        const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
        const half = cfg.kind === 'punch' ? cfg.diag / 2 : 0;
        const top = Math.max(...ys) + (cfg.kind === 'punch' ? cfg.clamp.height : 0);
        const minx = Math.min(...xs, -half), maxx = Math.max(...xs, half), miny = Math.min(...ys, 0), maxy = Math.max(top, cfg.kind === 'punch' ? cfg.diag : 0);
        const r = svg.getBoundingClientRect(), aspect = r.height / r.width || 0.75;
        view.w = Math.max((maxx - minx) + 12, ((maxy - miny) + 12) / aspect);
        view.cx = (minx + maxx) / 2; view.cy = (miny + maxy) / 2;
        render();
    }

    // ---- drawing -------------------------------------------------------------------------------------------
    function drawGrid(g, vb) {
        const x0 = Math.floor(vb.x), x1 = Math.ceil(vb.x + vb.w), y0 = Math.floor(-(vb.y + vb.h)), y1 = Math.ceil(-vb.y);
        const minor = view.w < 160;
        let d1 = '', d5 = '';
        for (let x = x0; x <= x1; x++) { if (x % GRID === 0) d5 += `M${x} ${-y0}V${-y1}`; else if (minor) d1 += `M${x} ${-y0}V${-y1}`; }
        for (let y = y0; y <= y1; y++) { if (y % GRID === 0) d5 += `M${x0} ${-y}H${x1}`; else if (minor) d1 += `M${x0} ${-y}H${x1}`; }
        if (d1) el('path', { d: d1, stroke: '#2a2f36', 'stroke-width': view.w / 1200, fill: 'none' }, g);
        el('path', { d: d5, stroke: '#39414b', 'stroke-width': view.w / 700, fill: 'none' }, g);
        el('path', { d: `M${x0} 0H${x1}M0 ${-y0}V${-y1}`, stroke: '#e0a526', 'stroke-opacity': 0.55, 'stroke-width': view.w / 600, fill: 'none' }, g);
    }
    function drawRaster(g) {   // 45 degree raster of 5 mm cells folded over the tip: what leg length fits beside the punch
        const step = GRID * SQ2, side = CELLS * step;
        let d = '';
        const pt = (p, q) => `${(q - p) / 2} ${-(p + q) / 2}`;
        for (let k = 0; k <= CELLS; k++) { const c = k * step; d += `M${pt(c, 0)}L${pt(c, side)}M${pt(0, c)}L${pt(side, c)}`; }
        el('path', { d, stroke: '#7f8c99', 'stroke-width': view.w / 900, fill: 'none', 'stroke-opacity': 0.8 }, g);
    }
    function clampPoly() {   // the standard tang, flush with the left edge of the seat line (same rule as bending.profile.clamp_polygon)
        const c = cfg.clamp, seat = Math.max(...dense.map(p => p[1]));
        const xl = Math.min(...dense.filter(p => seat - p[1] < 0.05).map(p => p[0])), xr = xl + c.width, k = c.chamfer;
        const n0 = seat + c.notch_from_seat, n1 = n0 + c.notch_height, d = c.notch_depth, top = seat + c.height;
        return [[xl, seat], [xr, seat], [xr, top - k], [xr - k, top], [xl + k, top], [xl, top - k], [xl, n1], [xl + d, n1], [xl + d, n0], [xl, n0]];
    }

    function render() {
        const vb = viewBox();
        svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
        svg.textContent = '';
        const g = el('g', {}, svg);
        drawGrid(g, vb);
        if (cfg.kind === 'punch' && $('pe-raster').checked) drawRaster(g);
        if (cfg.kind === 'punch' && dense.length) {
            const cp = clampPoly();
            el('path', { d: 'M' + cp.map(p => `${p[0]} ${-p[1]}`).join('L') + 'Z', fill: '#7d8a97', 'fill-opacity': 0.7, stroke: '#e0a526', 'stroke-opacity': 0.5,
                         'stroke-dasharray': `${view.w / 90} ${view.w / 140}`, 'stroke-width': view.w / 700, style: 'pointer-events:none' }, g);
        }
        el('path', { d: 'M' + dense.map(p => `${p[0]} ${-p[1]}`).join('L') + 'Z', fill: '#cfd8e0', 'fill-opacity': 0.9, stroke: '#e0a526', 'stroke-width': view.w / 450, 'stroke-linejoin': 'round' }, g);
        // the polygon of vertices (thin) shows where the sharp corners / chords are, behind the handles
        el('path', { d: 'M' + pts.map(p => `${p[0]} ${-p[1]}`).join('L') + 'Z', fill: 'none', stroke: '#5b6b7a', 'stroke-dasharray': `${view.w / 120} ${view.w / 160}`, 'stroke-width': view.w / 900, style: 'pointer-events:none' }, g);
        pts.forEach((p, i) => {   // edges: wide invisible hit-areas, click = insert a vertex
            const q = pts[(i + 1) % pts.length];
            const e = el('line', { x1: p[0], y1: -p[1], x2: q[0], y2: -q[1], stroke: 'transparent', 'stroke-width': view.w / 60, style: 'cursor:copy' }, g);
            e.addEventListener('pointerdown', ev => { ev.stopPropagation(); insertOnEdge(i, toWorld(ev)); });
        });
        const rad = view.w / 110;
        pts.forEach((p, i) => {
            const c = el('circle', { cx: p[0], cy: -p[1], r: i === sel ? rad * 1.4 : rad, fill: i === sel ? '#e0a526' : (p[2] > 0 ? '#2f6f9f' : '#17130a'),
                                     stroke: '#e0a526', 'stroke-width': view.w / 600, style: 'cursor:grab' }, g);
            c.addEventListener('pointerdown', ev => startDrag(ev, i));
            const t = el('text', { x: p[0] + rad * 1.6, y: -p[1] - rad * 1.2, 'font-size': view.w / 55, fill: '#9aa5b1', style: 'pointer-events:none;user-select:none' }, g);
            t.textContent = String(i + 1);
        });
        updateTable();
    }

    // ---- editing ----------------------------------------------------------------------------------------------
    function insertOnEdge(i, w) {
        const a = pts[i], b = pts[(i + 1) % pts.length];
        const dx = b[0] - a[0], dy = b[1] - a[1], len2 = dx * dx + dy * dy || 1;
        const t = Math.max(0, Math.min(1, ((w[0] - a[0]) * dx + (w[1] - a[1]) * dy) / len2));
        pushUndo();
        a[3] = 0;   // the edge is split: an arc on it would no longer fit
        pts.splice(i + 1, 0, [round(snapv(a[0] + t * dx)), round(snapv(a[1] + t * dy)), 0, 0]);
        sel = i + 1; changed();
    }
    function startDrag(ev, i) {
        ev.stopPropagation(); ev.preventDefault();
        sel = i; pushUndo();
        const move = e => { const w = toWorld(e); pts[i][0] = round(snapv(w[0])); pts[i][1] = round(snapv(w[1])); changed(); };
        const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
        window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
        render();
    }
    function removePoint(i) {
        if (pts.length <= 3) { toast('Профилът трябва да има поне 3 точки.', true); return; }
        pushUndo();
        pts[(i - 1 + pts.length) % pts.length][3] = 0;   // the neighbouring edge changes
        pts.splice(i, 1); sel = -1; changed();
    }
    function mirror() { pushUndo(); pts = pts.map(p => [-p[0], p[1], p[2], -p[3]]); changed(); }   // mirroring flips which side an arc bulges
    function zeroOrigin() {   // punch: lowest point of the outline -> origin; die: top edge centre -> origin
        pushUndo();
        const xs = dense.map(p => p[0]), ys = dense.map(p => p[1]);
        let ox, oy;
        if (cfg.kind === 'punch') {
            oy = Math.min(...ys);
            const tip = dense.filter(p => p[1] - oy < 0.05).map(p => p[0]);
            ox = tip.reduce((a, b) => a + b, 0) / tip.length;
        } else { oy = Math.max(...ys); ox = (Math.min(...xs) + Math.max(...xs)) / 2; }
        pts = pts.map(p => [round(p[0] - ox), round(p[1] - oy), p[2], p[3]]); changed();
    }

    // ---- side panel --------------------------------------------------------------------------------------------
    function updateTable() {
        const tb = $('pe-rows');
        tb.textContent = '';
        pts.forEach((p, i) => {
            const tr = document.createElement('tr');
            tr.className = i === sel ? 'sel' : '';
            tr.style.cursor = 'pointer';
            const td = t => { const c = document.createElement('td'); if (t !== undefined) c.textContent = t; tr.appendChild(c); return c; };
            td(i + 1);
            [0, 1, 2, 3].forEach(k => {
                const inp = document.createElement('input');
                inp.type = 'number'; inp.step = 'any'; inp.value = p[k];
                if (k === 2) { inp.min = 0; inp.title = 'Радиус на закръгляване в този ъгъл (0 = остър)'; }
                if (k === 3) inp.title = 'Радиус на дъга вместо правия ръб към следващата точка (0 = прав; + вдлъбната, − изпъкнала)';
                inp.addEventListener('focus', () => { sel = i; highlight(); });
                inp.addEventListener('change', () => {
                    const v = parseFloat(inp.value);
                    if (!isFinite(v) || (k === 2 && v < 0)) { inp.value = p[k]; return; }
                    pushUndo(); pts[i][k] = k < 2 ? round(v) : num(v); changed();
                });
                td().appendChild(inp);
            });
            const del = document.createElement('button');
            del.type = 'button'; del.className = 'btn-delete'; del.textContent = '×'; del.title = 'Изтрий точката';
            del.addEventListener('click', () => removePoint(i));
            td().appendChild(del);
            tr.addEventListener('click', () => { sel = i; highlight(); });
            tb.appendChild(tr);
        });
    }
    function highlight() {
        [...svg.querySelectorAll('circle')].forEach((c, i) => {
            const on = i === sel, rad = view.w / 110;
            c.setAttribute('r', on ? rad * 1.4 : rad); c.setAttribute('fill', on ? '#e0a526' : (pts[i][2] > 0 ? '#2f6f9f' : '#17130a'));
        });
        [...$('pe-rows').children].forEach((r, i) => r.classList.toggle('sel', i === sel));
    }
    function showInfo(d) {
        const x = d.derived;
        $('pe-width').textContent = x.width + ' mm';
        $('pe-height').textContent = x.height + ' mm';
        $('pe-area').textContent = x.area + ' mm²';
        $('pe-count').textContent = pts.length;
        if ($('pe-total')) $('pe-total').textContent = round(x.max_y + cfg.clamp.height) + ' mm';
        const L = $('pe-radii');
        L.textContent = '';
        d.radii.forEach(r => {
            const li = document.createElement('li');
            li.textContent = (r.kind === 'fillet' ? `Закръгляне в т. ${r.index}: ` : `Дъга ${r.index}→${r.index % pts.length + 1} (${r.concave ? 'вдлъбната' : 'изпъкнала'}): `) + 'R' + num(r.radius) + ' mm';
            L.appendChild(li);
        });
        if (!d.radii.length) { const li = document.createElement('li'); li.textContent = 'Няма радиуси - всички ъгли са остри.'; L.appendChild(li); }
        const D = $('pe-derived');
        D.textContent = '';
        const add = (k, v) => { if (v === null || v === undefined) return; const li = document.createElement('li'); li.textContent = k + ': ' + v; D.appendChild(li); };
        if (cfg.kind === 'punch') { add('Ъгъл на върха', x.tip_angle + '°'); add('Радиус на върха', x.tip_radius ? 'R' + x.tip_radius : 'остър'); }
        else {
            add('Ъгъл на V', x.valley_angle + '°'); add('V-отвор', x.v_opening + ' mm'); add('Дълбочина на канала', x.groove_depth + ' mm');
            add('Радиус на дъното', x.valley_radius ? 'R' + x.valley_radius : 'остър');
            add('Радиуси на раменете', (x.shoulder_radii || []).map(r => r ? 'R' + r : 'остър').join(' / '));
        }
        const W = $('pe-warn');
        W.textContent = '';
        d.warnings.forEach(w => { const li = document.createElement('li'); li.textContent = w; W.appendChild(li); });
        W.parentElement.style.display = d.warnings.length ? '' : 'none';
    }
    function toast(msg, bad) {
        const t = $('pe-msg');
        t.textContent = msg; t.className = 'pe-msg ' + (bad ? 'bad' : 'good');
    }

    // ---- save / reset / DXF -----------------------------------------------------------------------------------
    async function post(url, body) {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN }, body: JSON.stringify(body) });
        return { ok: r.ok, data: await r.json().catch(() => ({})) };
    }
    async function save() {
        const r = await post(cfg.saveUrl, { points: pts });
        if (r.ok) { dirty = false; toast('Профилът е запазен.'); } else toast(r.data.message || 'Грешка при запис.', true);
    }
    async function reset() {
        if (!confirm('Връщане към начален контур по числата на инструмента? Запазеният профил ще се изтрие.')) return;
        const r = await post(cfg.saveUrl, { reset: true });
        if (r.ok) { dirty = false; location.href = cfg.backUrl; } else toast(r.data.message || 'Грешка.', true);
    }
    async function loadDxf(file) {
        const fd = new FormData();
        fd.append('file', file); fd.append('csrf_token', window.CSRF_TOKEN);
        const res = await fetch(cfg.dxfUrl, { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.message || 'Невалиден DXF.', true); return; }
        pushUndo(); pts = data.points; sel = -1; await refresh(); fit();
        toast(`Прочетени са ${pts.length} точки от DXF (дъгите са запазени с радиусите си) - прегледайте и запазете.`);
    }

    // ---- wiring ----------------------------------------------------------------------------------------------------
    $('pe-snap').addEventListener('change', e => { snap = parseFloat(e.target.value); });
    if ($('pe-raster')) $('pe-raster').addEventListener('change', render);
    $('pe-undo').addEventListener('click', undo);
    $('pe-redo').addEventListener('click', redo);
    $('pe-mirror').addEventListener('click', mirror);
    $('pe-zero').addEventListener('click', zeroOrigin);
    $('pe-fit').addEventListener('click', fit);
    $('pe-del').addEventListener('click', () => { if (sel >= 0) removePoint(sel); else toast('Изберете точка.', true); });
    $('pe-save').addEventListener('click', save);
    $('pe-reset').addEventListener('click', reset);
    $('pe-dxf').addEventListener('change', e => { if (e.target.files[0]) loadDxf(e.target.files[0]); e.target.value = ''; });
    svg.addEventListener('wheel', ev => {
        ev.preventDefault();
        const w = toWorld(ev), k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
        view.cx = w[0] + (view.cx - w[0]) * k; view.cy = w[1] + (view.cy - w[1]) * k; view.w *= k; render();
    }, { passive: false });
    svg.addEventListener('pointerdown', ev => {   // drag on the background = pan
        if (ev.target !== svg && ev.target.tagName !== 'path') return;
        sel = -1;
        let last = [ev.clientX, ev.clientY];
        const move = e => {
            const r = svg.getBoundingClientRect(), k = view.w / r.width;
            view.cx -= (e.clientX - last[0]) * k; view.cy += (e.clientY - last[1]) * k; last = [e.clientX, e.clientY]; render();
        };
        const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
        window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
    });
    svg.addEventListener('pointermove', ev => { const w = toWorld(ev); $('pe-cursor').textContent = `X ${round(w[0])}  Y ${round(w[1])} mm`; });
    document.addEventListener('keydown', ev => {
        if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'SELECT') return;
        if ((ev.key === 'Delete' || ev.key === 'Backspace') && sel >= 0) { ev.preventDefault(); removePoint(sel); }
        else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'z') { ev.preventDefault(); ev.shiftKey ? redo() : undo(); }
        else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'y') { ev.preventDefault(); redo(); }
        else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') { ev.preventDefault(); save(); }
    });
    window.addEventListener('beforeunload', ev => { if (dirty) { ev.preventDefault(); ev.returnValue = ''; } });
    window.addEventListener('resize', render);
    fit();   // draw at once from the vertices; the server's real outline (radii expanded) follows
    refresh().then(fit);
})();
