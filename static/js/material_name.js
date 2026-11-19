/* Composed material name (owner's decision 2026-11-19).
   composeMaterialName() mirrors compose_material_name() in app.py - keep both in sync by hand:
   Тип (ед. число) · "Калиброван" · Вид · Марка · Размери · Калибрация · Забележка, empty parts skipped.
   Размери: round type "⌀" + width × length, else width × height × length, then " N мм" thickness -
   only the dimensions the type has. cfg = one entry of material_types_js(). */
(function () {
    function num3(v) {
        const x = parseFloat(v);
        return isFinite(x) && x ? String(+x.toFixed(3)) : '';
    }

    function composeMaterialName(cfg, p) {
        cfg = cfg || {};
        const dims = cfg.dims || [];
        const w = num3(p.width), h = num3(p.height), l = num3(p.length), t = num3(p.thickness);
        let size = [dims[1] && w ? (cfg.round ? '⌀' : '') + w : '', cfg.height && h ? h : '', dims[0] && l ? l : '']
            .filter(Boolean).join('×');
        if (dims[2] && t) size = (size + ' ' + t + ' мм').trim();
        return [cfg.singular || cfg.label, p.calibrated ? 'Калиброван' : '', p.grade, p.brand, size,
                p.calibrated ? p.calibration : '', p.notes]
            .map(function (s) { return (s || '').trim(); }).filter(Boolean).join(' ').slice(0, 100);
    }

    /* Keeps a form's name input filled from its parts until the user types in it; ids maps
       name/type/grade/brand/width/height/length/thickness/calibrated/calibration/notes/refresh to element ids
       (missing ones are skipped). The "Калибрация" input's .form-group shows only while "Калиброван" is ticked.
       active() (optional) - false = leave the name alone. Returns {reset(touched), sync()}. */
    function bindMaterialName(types, ids, active) {
        const el = function (k) { return ids[k] ? document.getElementById(ids[k]) : null; };
        const nameEl = el('name');
        let touched = false;
        function val(k) { const e = el(k); return e ? e.value : ''; }
        function gradeText() {
            const s = el('grade');
            return s && s.value ? s.selectedOptions[0].textContent.replace(/\s*\([^)]*\)\s*$/, '').trim() : '';
        }
        function composed() {
            const t = el('type'), cb = el('calibrated');
            return composeMaterialName(types[t ? t.value : 'sheets'] || {}, {
                calibrated: cb && cb.checked, calibration: val('calibration'), grade: gradeText(), brand: val('brand'),
                width: val('width'), height: val('height'), length: val('length'), thickness: val('thickness'),
                notes: val('notes'),
            });
        }
        function sync() {
            const cb = el('calibrated'), cal = el('calibration');
            if (cb && cal) (cal.closest('.form-group') || cal).style.display = cb.checked ? '' : 'none';
            if (!touched && nameEl && (!active || active())) nameEl.value = composed();
        }
        ['type', 'grade', 'brand', 'width', 'height', 'length', 'thickness', 'calibrated', 'calibration', 'notes']
            .forEach(function (k) {
                const e = el(k);
                if (e) { e.addEventListener('input', sync); e.addEventListener('change', sync); }
            });
        if (nameEl) nameEl.addEventListener('input', function () { touched = true; });
        const refresh = el('refresh');
        if (refresh) refresh.addEventListener('click', function () { touched = false; sync(); });
        sync();
        return {
            reset: function (t) { touched = !!t; sync(); },
            sync: sync,
            composed: composed,
        };
    }

    window.composeMaterialName = composeMaterialName;
    window.bindMaterialName = bindMaterialName;
})();
