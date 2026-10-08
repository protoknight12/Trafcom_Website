// Consumption panel attached to one meter's card: energy / cost / saving cards + one plate with stacked power, voltage and
// current charts on a shared time axis (hover shows every reading at that moment).
// <div data-energy-panel data-key="<meter key, see _meter_log_keys()>" data-url="/admin/energy-report"></div>
// The live page re-renders its cards every few seconds, so per-meter state (period, last answer) lives here and a mount
// repaints from it and only refetches when the answer is older than a minute.
var EnergyPanel = (function () {
    var PERIODS = [['today', 'Днес'], ['7d', '7 дни'], ['30d', '30 дни'], ['month', 'Този месец'],
                   ['billing', 'Текущ период (отчет)'], ['billing_prev', 'Предишен период (отчет)'], ['custom', 'От-до']];
    var CHARTS = [['power', 'Мощност', 'W', '#b07cff'], ['soc', 'Заряд на батерията', '%', '#10b981'], ['voltage', 'Напрежение (средно)', 'V', '#3fa7ff'], ['current', 'Ток (сума по фази)', 'A', '#ffb02e']];
    var TIPS = {
        total: 'Консумирана електроенергия за периода и цената ѝ по дневна/нощна тарифа (цените се задават в страницата на термопомпата).',
        grid: 'Част от енергията, взета от мрежата (внос на външния CT на Solis спрямо цялото потребление на шината). Цената е само за този дял.',
        solar: 'Част от енергията от слънцето - директно или през батерията. Цената е това, което щеше да струва от мрежата - спестената сума.',
        src_unknown: 'Минути без запис от инверторите - не може да се раздели между мрежа и слънце.'
    };
    var STALE_MS = 60000, state = {};

    var st = document.createElement('style');
    st.textContent = '.ep{margin-top:14px;border-top:1px solid var(--border-color);padding-top:10px}' +
        '.ep-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:8px 0}' +
        '.ep-card{background:var(--bg-input);border:1px solid var(--border-color);border-radius:var(--border-radius-sm);padding:10px 14px}' +
        '.ep-card strong{font-size:1.3rem}' +
        '.ep-legend{display:flex;gap:14px;flex-wrap:wrap;margin:4px 0;font-size:.82rem}.ep-legend span{cursor:pointer;user-select:none}.ep-legend span.off{opacity:.4;text-decoration:line-through}.ep-plate{position:relative;background:var(--bg-input);border:1px solid var(--border-color);border-radius:var(--border-radius-sm);padding:6px}' +
        '.ep-plate canvas{width:100%;display:block}' +
        '.ep-tip{position:absolute;top:8px;display:none;pointer-events:none;background:var(--bg-main,#111);color:var(--text-main);border:1px solid var(--border-color);border-radius:6px;padding:6px 9px;font-size:.8rem;white-space:nowrap;z-index:5}' +
        '.ep-line{position:absolute;top:6px;bottom:6px;width:1px;background:rgba(128,128,128,.6);display:none;pointer-events:none}' +
        '.ep-period,.ep-from,.ep-to{padding:3px 6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border-color);border-radius:4px}';
    document.head.appendChild(st);

    function card(label, tip, cost, kwh) {
        return '<div class="ep-card" title="' + tip + '"><div class="text-muted text-small">' + label + '</div><strong>' + cost.toFixed(2) +
            ' <small>€</small></strong><div class="text-muted text-small">' + kwh.toFixed(2) + ' kWh</div></div>';
    }

    function available(series) {
        return CHARTS.filter(function (c) { return (series[c[0]] || []).some(function (p) { return p[1] != null; }); });
    }

    // Draws the stacked charts; returns the layout the tooltip needs.
    function draw(cv, series, hidden) {
        var active = available(series).filter(function (c) { return !(hidden || {})[c[0]]; });
        var dpr = window.devicePixelRatio || 1, w = cv.clientWidth, rowH = 96, h = Math.max(1, active.length) * rowH + 18;
        cv.style.height = h + 'px'; cv.width = w * dpr; cv.height = h * dpr;
        var c = cv.getContext('2d'); c.scale(dpr, dpr); c.clearRect(0, 0, w, h);
        c.font = '11px sans-serif'; c.fillStyle = '#888';
        var ref = series.power || [];
        if (!active.length || !ref.length) { c.fillText(ref.length ? 'Всички графики са изключени - включи от легендата.' : 'Няма записани данни за периода.', 10, 20); return null; }
        var t0 = ref[0][0], t1 = ref[ref.length - 1][0], L = 46, R = 8, T = 14, B = 4;
        function X(t) { return L + (t - t0) / Math.max(1, t1 - t0) * (w - L - R); }
        active.forEach(function (ch, k) {
            var pts = series[ch[0]], y0 = k * rowH, vals = pts.filter(function (p) { return p[1] != null; }).map(function (p) { return p[1]; });
            var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max.apply(null, vals);
            if (hi === lo) hi = lo + 1;
            function Y(v) { return y0 + T + (hi - v) / (hi - lo) * (rowH - T - B - 8); }
            c.fillStyle = ch[3]; c.textAlign = 'left'; c.fillText(ch[1] + ', ' + ch[2], L, y0 + 10);
            c.fillStyle = '#888'; c.strokeStyle = 'rgba(128,128,128,.25)';
            for (var i = 0; i <= 2; i++) {
                var v = lo + (hi - lo) * i / 2;
                c.beginPath(); c.moveTo(L, Y(v)); c.lineTo(w - R, Y(v)); c.stroke();
                c.fillText(hi >= 100 ? Math.round(v) : v.toFixed(1), 2, Y(v) + 4);
            }
            c.strokeStyle = ch[3]; c.lineWidth = 1.5; c.beginPath();
            var started = false;
            pts.forEach(function (p) {
                if (p[1] == null) { started = false; return; }
                if (started) c.lineTo(X(p[0]), Y(p[1])); else { c.moveTo(X(p[0]), Y(p[1])); started = true; }
            });
            c.stroke();
        });
        c.fillStyle = '#888';
        for (var i = 0; i <= 4; i++) {
            var t = t0 + (t1 - t0) * i / 4;
            c.fillText(new Date(t * 1000).toLocaleString('bg-BG', t1 - t0 > 86400 * 1.5 ? {day: '2-digit', month: '2-digit'} : {hour: '2-digit', minute: '2-digit'}), X(t) - 14, h - 4);
        }
        return {t0: t0, t1: t1, L: L, R: R, w: w, active: active, series: series};
    }

    function hover(plate, cv, getLayout) {
        var tip = plate.querySelector('.ep-tip'), line = plate.querySelector('.ep-line');
        function hide() { tip.style.display = line.style.display = 'none'; }
        cv.addEventListener('mouseleave', hide);
        cv.addEventListener('mousemove', function (e) {
            var H = getLayout(); if (!H) return hide();
            var x = e.clientX - cv.getBoundingClientRect().left;
            if (x < H.L || x > H.w - H.R) return hide();
            var t = H.t0 + (x - H.L) / (H.w - H.L - H.R) * (H.t1 - H.t0), ref = H.series.power, best = 0, bd = Infinity;
            ref.forEach(function (p, i) { var d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = i; } });
            var lx = H.L + (ref[best][0] - H.t0) / Math.max(1, H.t1 - H.t0) * (H.w - H.L - H.R);
            var html = '<b>' + new Date(ref[best][0] * 1000).toLocaleString('bg-BG', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) + '</b>';
            H.active.forEach(function (ch) {
                var p = H.series[ch[0]][best], v = p && p[1] != null ? p[1] + ' ' + ch[2] : '—';
                html += '<br><span style="color:' + ch[3] + '">●</span> ' + ch[1].split(' (')[0] + ': ' + v;
            });
            tip.innerHTML = html; tip.style.display = line.style.display = 'block';
            line.style.left = lx + 'px';
            tip.style.left = (lx + 12 + tip.offsetWidth > H.w ? lx - 12 - tip.offsetWidth : lx + 12) + 'px';
        });
    }

    function mount(el) {
        var key = el.dataset.key, S = state[key] || (state[key] = {hidden: {}, period: 'today', from: '', to: '', data: null, at: 0, busy: false});
        var today = new Date().toISOString().slice(0, 10);
        S.from = S.from || today; S.to = S.to || today;
        el.classList.add('ep');
        el.innerHTML = '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">' +
            '<strong>Консумация и разход</strong><select class="ep-period">' + PERIODS.map(function (p) { return '<option value="' + p[0] + '">' + p[1] + '</option>'; }).join('') + '</select>' +
            '<span class="ep-range" style="display:none"><input type="date" class="ep-from"> – <input type="date" class="ep-to"></span></div>' +
            '<div class="ep-grid"></div><p class="text-muted text-small ep-note"></p><div class="ep-legend"></div>' +
            '<div class="ep-plate"><canvas></canvas><div class="ep-line"></div><div class="ep-tip"></div></div>';
        var q = function (s) { return el.querySelector(s); }, layout = null;
        q('.ep-period').value = S.period; q('.ep-from').value = S.from; q('.ep-to').value = S.to;
        q('.ep-range').style.display = S.period === 'custom' ? 'inline' : 'none';
        hover(q('.ep-plate'), q('canvas'), function () { return layout; });

        function legend() {
            var lg = q('.ep-legend');
            lg.innerHTML = S.data && S.data.series ? available(S.data.series).map(function (c) {
                return '<span data-k="' + c[0] + '" class="' + (S.hidden[c[0]] ? 'off' : '') + '"><i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:' + c[3] + ';margin-right:5px"></i>' + c[1].split(' (')[0] + '</span>';
            }).join('') : '';
            lg.querySelectorAll('span').forEach(function (sp) {
                sp.addEventListener('click', function () { S.hidden[sp.dataset.k] = !S.hidden[sp.dataset.k]; paint(); });
            });
        }
        function paint() {
            var j = S.data;
            if (!j) { q('.ep-note').textContent = 'Зареждане…'; return; }
            if (j.error) { q('.ep-grid').innerHTML = ''; q('.ep-note').textContent = j.error; layout = null; draw(q('canvas'), {}); legend(); return; }
            if (!j.cost) { q('.ep-grid').innerHTML = ''; q('.ep-note').textContent = ''; layout = draw(q('canvas'), j.series, S.hidden); legend(); return; }
            var C = j.cost, rows = [card('Консумация', TIPS.total, C.total.cost, C.total.kwh),
                card('От мрежата', TIPS.grid, C.grid.cost, C.grid.kwh),
                card('Спестено от слънцето (и батерията)', TIPS.solar, C.solar.cost, C.solar.kwh)];
            if (C.src_unknown.kwh > 0) rows.push(card('Без данни за източника', TIPS.src_unknown, C.src_unknown.cost, C.src_unknown.kwh));
            q('.ep-grid').innerHTML = rows.join('');
            q('.ep-note').textContent = j.priced ? '' : 'Цените на тока не са зададени - задай ги в страницата на термопомпата, за да се изчисляват разходът и спестяването.';
            layout = draw(q('canvas'), j.series, S.hidden); legend();
        }
        function load(force) {
            if (S.busy || (!force && S.data && Date.now() - S.at < STALE_MS)) return;
            var url = el.dataset.url + '?key=' + encodeURIComponent(key) + '&period=' + S.period;
            if (S.period === 'custom') url += '&from=' + S.from + '&to=' + S.to;
            S.busy = true;
            fetch(url).then(function (r) { return r.json(); }).then(function (j) { S.data = j; S.at = Date.now(); paint(); })
                .catch(function () {}).then(function () { S.busy = false; });
        }
        [['.ep-period', 'period'], ['.ep-from', 'from'], ['.ep-to', 'to']].forEach(function (f) {
            q(f[0]).addEventListener('change', function () {
                S[f[1]] = this.value; q('.ep-range').style.display = S.period === 'custom' ? 'inline' : 'none'; load(true);
            });
        });
        paint(); load(false);
    }

    function mountAll(root) { (root || document).querySelectorAll('[data-energy-panel]').forEach(mount); }
    document.addEventListener('DOMContentLoaded', function () { mountAll(document); });
    setInterval(function () { Object.keys(state).forEach(function (k) { state[k].at = 0; }); }, STALE_MS);   // next repaint refetches
    return {mountAll: mountAll};
})();
