// Raw-material quantity <-> kg. Mirrors app.py: material_density(), material_kg_per_unit(),
// material_kg_per_stock_unit(). Change both together (testing/test_material_kg_entry.py compares them).
(function (root) {
    // Mirrors DEFAULT_MATERIAL_GRADES in app.py (grade name, kg/dm3, keywords; first match wins) - keywords + density only.
    var DENSITY_GUESS = [
        [['неръжд', 'инокс', 'inox', 'aisi', 'stainless'], 7.93],
        [['алум', 'alu'], 2.70],
        [['месинг', 'brass'], 8.50],
        [['мед', 'copper'], 8.96],
        [['бронз', 'bronze'], 8.80],
        [['титан', 'titan'], 4.51],
        [['стомана', 'желязо', 'поцинк', 'ламарина', 'steel', 'dc01', 's235', 's355', 'hardox'], 7.85]
    ];

    function guessDensity(name) {
        var n = String(name || '').toLowerCase();
        for (var i = 0; i < DENSITY_GUESS.length; i++) {
            if (DENSITY_GUESS[i][0].some(function (w) { return n.indexOf(w) !== -1; })) return DENSITY_GUESS[i][1];
        }
        return null;
    }

    function sheetArea(p) {   // one sheet, m2 (null without a size)
        return p.width && p.length ? p.width * p.length / 1e6 : null;
    }

    // kg per NATIVE unit: kg/m2 ('m2'), kg/m ('m'), kg/piece ('pcs'); null = unknown.
    // p = {unit, round, width, length, thickness, height, weight, density}
    function kgPerUnit(p) {
        var weight = p.weight || null;
        if (p.unit === 'pcs') return weight;
        if (p.unit === 'm2') {
            var area = sheetArea(p);
            if (weight && area) return weight / area;
        } else if (weight) {
            return weight;
        }
        if (!p.density) return null;
        var t = p.thickness || 0;
        if (p.unit === 'm2') return t ? p.density * t : null;
        var w = p.width || 0;
        if (!w) return null;
        var section;
        if (p.round) {
            var inner = t ? Math.max(0, w - 2 * t) : 0;
            section = Math.PI / 4 * (w * w - inner * inner);
        } else {
            var h = p.height || w;
            section = w * h - (t ? Math.max(0, w - 2 * t) * Math.max(0, h - 2 * t) : 0);
        }
        return p.density * section / 1000;
    }

    // kg of ONE stock unit (a sheet, a metre, a piece); a sheet lot without a size counts per m2.
    function kgPerStockUnit(p) {
        var per = kgPerUnit(p);
        if (per === null) return null;
        return p.unit === 'm2' ? per * (sheetArea(p) || 1) : per;
    }

    function num(el) {
        var v = parseFloat(el && el.value);
        return isFinite(v) ? v : null;
    }

    function round(x, d) {
        var f = Math.pow(10, d);
        return Math.round(x * f) / f;
    }

    // Pairs a quantity input with a kg input. getKg() returns kg per one quantity unit (null = unknown,
    // undefined = not a material line: `group` is hidden). Typing kg fills the quantity and the other way
    // round; call refresh() after the quantity or the parameters were set from code.
    function bind(qtyEl, kgEl, getKg, group, watch) {
        var perUnit = null;
        function refresh() {
            var k = getKg();
            if (group) group.style.display = k === undefined ? 'none' : '';
            perUnit = k || null;
            kgEl.disabled = !perUnit;
            kgEl.placeholder = perUnit ? 'кг' : 'няма плътност';
            kgEl.title = perUnit ? '' : 'Няма плътност или тегло за този материал - задайте ги в материала';
            var q = parseFloat(qtyEl.value);
            kgEl.value = perUnit && q > 0 ? round(q * perUnit, 3) : '';
        }
        qtyEl.addEventListener('input', refresh);
        qtyEl.addEventListener('change', refresh);
        kgEl.addEventListener('input', function () {
            var kg = parseFloat(kgEl.value);
            if (!perUnit || !(kg > 0)) return;
            qtyEl.value = round(kg / perUnit, 4);
            qtyEl.dispatchEvent(new Event('qty-from-kg'));
        });
        (watch || []).forEach(function (el) {
            el.addEventListener('input', refresh);
            el.addEventListener('change', refresh);
        });
        var form = qtyEl.form;
        if (form) form.addEventListener('reset', function () { setTimeout(refresh, 0); });
        refresh();
        return { refresh: refresh };
    }

    // Params from a material <option> (data-type/-w/-h/-t/-density/-weight/-hm, see partials/material_options.html).
    // types = material_types_js(); dims = optional {width, length, thickness} typed on the line.
    // A measured weight belongs to the catalog size: a line edited to another size falls back to density.
    function optionParams(opt, types, dims) {
        var d = opt.dataset, cfg = types[d.type] || {};
        function n(v) { var x = parseFloat(v); return isFinite(x) && x ? x : null; }
        var p = {unit: cfg.unit || 'm2', round: !!cfg.round, width: n(d.w), length: n(d.h), thickness: n(d.t),
                 height: n(d.hm), weight: n(d.weight), density: n(d.density)};
        if (dims) {
            var changed = (dims.width || null) !== p.width || (dims.length || null) !== p.length ||
                          (dims.thickness || null) !== p.thickness;
            if (changed && p.unit !== 'pcs') p.weight = null;
            p.width = dims.width || null;
            p.length = dims.length || null;
            p.thickness = dims.thickness || null;
        }
        return p;
    }

    var api = {guessDensity: guessDensity, kgPerUnit: kgPerUnit, kgPerStockUnit: kgPerStockUnit, bind: bind,
               optionParams: optionParams, sheetArea: sheetArea, num: num};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.MaterialKg = api;
})(typeof window !== 'undefined' ? window : this);
