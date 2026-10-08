// Consumption panel for one meter: power / voltage / current charts + energy, cost and saving for a period.
// <div data-energy-panel data-kind="shelly|modbus" data-id="7" data-name="..." data-url="/admin/energy-report"></div>
(function () {
    var PERIODS = [['today', 'Днес'], ['7d', '7 дни'], ['30d', '30 дни'], ['month', 'Този месец'],
                   ['billing', 'Текущ период (отчет)'], ['billing_prev', 'Предишен период (отчет)'], ['custom', 'От-до']];
    var CHARTS = [['power', 'Мощност', 'W', '#b07cff'], ['voltage', 'Напрежение (средно)', 'V', '#3fa7ff'], ['current', 'Ток (сума по фази)', 'A', '#ffb02e']];
    var TIPS = {
        total: 'Консумирана електроенергия за периода и цената ѝ по дневна/нощна тарифа (цените се задават в страницата на термопомпата).',
        grid: 'Част от енергията, взета от мрежата (внос на външния CT на Solis спрямо цялото потребление на шината). Цената е само за този дял.',
        solar: 'Част от енергията от слънцето - директно или през батерията. Цената е това, което щеше да струва от мрежата - спестената сума.',
        src_unknown: 'Минути без запис от инверторите - не може да се раздели между мрежа и слънце.'
    };

    function card(label, tip, cost, kwh) {
        return '<div class="ep-card" title="' + tip + '"><div class="text-muted text-small">' + label + '</div><strong>' + cost.toFixed(2) +
            ' <small>€</small></strong><div class="text-muted text-small">' + kwh.toFixed(2) + ' kWh</div></div>';
    }

    function draw(cv, points, color, unit) {
        var dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
        cv.width = w * dpr; cv.height = h * dpr;
        var c = cv.getContext('2d'); c.scale(dpr, dpr); c.clearRect(0, 0, w, h);
        c.font = '11px sans-serif'; c.fillStyle = '#888';
        var pts = points.filter(function (p) { return p[1] != null; });
        if (!pts.length) { c.fillText('Няма записани данни за периода.', 10, 20); return; }
        var t0 = pts[0][0], t1 = pts[pts.length - 1][0], lo = Math.min(0, Math.min.apply(null, pts.map(function (p) { return p[1]; }))),
            hi = Math.max.apply(null, pts.map(function (p) { return p[1]; }));
        if (hi === lo) hi = lo + 1;
        var L = 46, B = 18, T = 6, R = 8;
        function X(t) { return L + (t - t0) / Math.max(1, t1 - t0) * (w - L - R); }
        function Y(v) { return T + (hi - v) / (hi - lo) * (h - T - B); }
        c.strokeStyle = 'rgba(128,128,128,.25)';
        for (var i = 0; i <= 4; i++) {
            var v = lo + (hi - lo) * i / 4;
            c.beginPath(); c.moveTo(L, Y(v)); c.lineTo(w - R, Y(v)); c.stroke();
            c.fillText(Math.abs(hi) >= 100 ? Math.round(v) : v.toFixed(1), 2, Y(v) + 4);
        }
        for (i = 0; i <= 4; i++) {
            var t = t0 + (t1 - t0) * i / 4;
            c.fillText(new Date(t * 1000).toLocaleString('bg-BG', t1 - t0 > 86400 * 1.5 ? {day: '2-digit', month: '2-digit'} : {hour: '2-digit', minute: '2-digit'}), X(t) - 14, h - 4);
        }
        c.strokeStyle = color; c.lineWidth = 1.6; c.beginPath();
        var started = false;
        points.forEach(function (p) {
            if (p[1] == null) { started = false; return; }
            if (started) c.lineTo(X(p[0]), Y(p[1])); else { c.moveTo(X(p[0]), Y(p[1])); started = true; }
        });
        c.stroke();
        c.fillStyle = color; c.fillText(unit, w - R - 14, T + 10);
    }

    var st = document.createElement('style');
    st.textContent = '.ep-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:10px}' +
        '.ep-card{background:var(--bg-input);border:1px solid var(--border-color);border-radius:var(--border-radius-sm);padding:12px 14px}' +
        '.ep-card strong{font-size:1.4rem}.ep-period,.ep-from,.ep-to{padding:3px 6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border-color);border-radius:4px}';
    document.head.appendChild(st);

    function mount(el) {
        var uid = Math.random().toString(36).slice(2, 8);
        el.innerHTML = '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px;">' +
            '<select class="ep-period">' + PERIODS.map(function (p) { return '<option value="' + p[0] + '">' + p[1] + '</option>'; }).join('') + '</select>' +
            '<span class="ep-range" style="display:none"><input type="date" class="ep-from"> – <input type="date" class="ep-to"></span></div>' +
            '<div class="ep-grid ep-cost"></div><p class="text-muted text-small ep-note"></p>' +
            CHARTS.map(function (c) {
                return '<div class="text-small text-muted" style="margin-top:8px;">' + c[1] + ', ' + c[2] + '</div><canvas class="ep-chart-' + c[0] + '" style="width:100%;height:120px;"></canvas>';
            }).join('');
        var q = function (s) { return el.querySelector(s); };
        q('.ep-from').value = q('.ep-to').value = new Date().toISOString().slice(0, 10);

        function load() {
            var period = q('.ep-period').value, url = el.dataset.url + '?kind=' + el.dataset.kind + '&id=' + el.dataset.id + '&period=' + period;
            q('.ep-range').style.display = period === 'custom' ? 'inline' : 'none';
            if (period === 'custom') url += '&from=' + q('.ep-from').value + '&to=' + q('.ep-to').value;
            fetch(url).then(function (r) { return r.json(); }).then(function (j) {
                if (j.error) { q('.ep-cost').innerHTML = ''; q('.ep-note').textContent = j.error; return; }
                var C = j.cost, rows = [card('Консумация', TIPS.total, C.total.cost, C.total.kwh),
                    card('От мрежата', TIPS.grid, C.grid.cost, C.grid.kwh),
                    card('Спестено от слънцето (и батерията)', TIPS.solar, C.solar.cost, C.solar.kwh)];
                if (C.src_unknown.kwh > 0) rows.push(card('Без данни за източника', TIPS.src_unknown, C.src_unknown.cost, C.src_unknown.kwh));
                q('.ep-cost').innerHTML = rows.join('');
                q('.ep-note').textContent = j.priced ? '' : 'Цените на тока не са зададени - задай ги в страницата на термопомпата, за да се изчисляват разходът и спестяването.';
                CHARTS.forEach(function (c) { draw(q('.ep-chart-' + c[0]), j.series[c[0]], c[3], c[2]); });
            });
        }
        ['.ep-period', '.ep-from', '.ep-to'].forEach(function (s) { q(s).addEventListener('change', load); });
        load(); setInterval(load, 60000);
    }

    document.querySelectorAll('[data-energy-panel]').forEach(mount);
})();
