// Picker window for <select data-picker="material|detail|product"> (owner's decision 2026-11-19: picking a
// material, detail or product anywhere opens a window with the whole list and its filters instead of the native
// drop-down). Loaded by partials/navbar.html, together with cascade_filters.js.
//
// The <select> stays the source of truth: a pick sets select.value and fires 'change' (bubbles), exactly like a
// user pick, so page handlers, form_dialog.js edit mode, the data-create "+" button and form posts are unchanged.
// The list is built from the select's CURRENT options on every open (hidden / disabled options are left out);
// <optgroup> = group rows, an empty-value option = "Без избор", a "__new__" option = an action button on top.
// Columns and filters come from the options' data-* attributes; one that no option carries is not shown.
// data-picker="off" (or a disabled / multiple select) keeps the native drop-down. Text only via textContent.
(function () {
    const TITLES = { material: 'Избор на материал', detail: 'Избор на детайл', product: 'Избор на продукт' };

    function num(v) {   // '2.0' -> '2', '0.94200' -> '0.942'; non-numbers unchanged
        return (v === undefined || v === '' || isNaN(v)) ? (v || '') : String(+(+v).toFixed(3));
    }
    function dimsText(d) {
        const w = num(d.w), h = num(d.h);
        if (w && h) return w + ' × ' + h;
        return w || h || '';
    }
    function stockText(d) {
        if (d.stock === undefined || d.stock === '') return '';
        return (d.stockText || num(d.stock)) + (parseFloat(d.stockKg) ? ' (≈ ' + num(d.stockKg) + ' кг)' : '');
    }
    function ownerText(d) { return d.ownerName || ''; }

    // [heading, value(option, groupLabel)], facet: show a filter select for it
    const COLUMNS = {
        material: [
            { label: 'Тип', get: function (o, g) { return o.dataset.type ? (g || '') : ''; }, facet: true, column: false },
            { label: 'Вид', get: function (o) { return o.dataset.grade || ''; }, facet: true },
            { label: 'Марка', get: function (o) { return o.dataset.brand || ''; }, facet: true },
            { label: 'Дебелина', get: function (o) { return num(o.dataset.t); }, facet: true },
            { label: 'Размер', get: function (o) { return dimsText(o.dataset); } },
            { label: 'Наличност', get: function (o) { return stockText(o.dataset); } },
            { label: 'Собственик', get: function (o) { return ownerText(o.dataset); }, facet: true },
        ],
        detail: [
            { label: 'Материал', get: function (o) { return o.dataset.material || ''; }, facet: true },
            { label: 'Собственик', get: function (o) { return ownerText(o.dataset); }, facet: true },
            { label: 'Цена (€)', get: function (o) { return o.dataset.price || ''; } },
        ],
        product: [
            { label: 'Собственик', get: function (o) { return ownerText(o.dataset); }, facet: true },
            { label: 'Цена (€)', get: function (o) { return o.dataset.price || ''; } },
        ],
    };

    function kindOf(select) {
        const k = select && select.dataset.picker;
        return (TITLES[k] && !select.disabled && !select.multiple && !(select.size > 1)) ? k : null;
    }
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    function open(select) {
        const kind = kindOf(select);
        if (!kind) return;
        const cols = COLUMNS[kind];
        const dlg = el('dialog', 'form-dialog entity-picker');
        const close = el('button', 'dialog-close', '×');
        close.type = 'button';
        close.setAttribute('aria-label', 'Затвори');
        close.addEventListener('click', function () { dlg.close(); });
        dlg.appendChild(close);
        dlg.appendChild(el('h3', null, TITLES[kind]));

        // --- collect options ---
        const items = [], actions = [];
        let none = null;
        Array.prototype.forEach.call(select.options, function (o) {
            if (o.hidden || o.disabled) return;
            if (o.value === '__new__') { actions.push(o); return; }
            if (o.value === '') { none = o; return; }
            const g = o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : '';
            items.push({ opt: o, group: g, values: cols.map(function (c) { return c.get(o, g); }) });
        });
        const shown = cols.map(function (c, i) {
            return items.some(function (it) { return it.values[i]; });
        });

        const pick = function (o) {
            dlg.close();
            if (select.value !== o.value) {
                select.value = o.value;
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }
            select.focus();
        };

        // --- toolbar: search + facets ---
        const bar = el('div', 'ep-bar');
        const search = el('input', 'ep-search');
        search.type = 'search';
        search.placeholder = 'Търсене...';
        search.setAttribute('aria-label', 'Търсене');
        bar.appendChild(search);
        const facets = [];
        cols.forEach(function (c, i) {
            if (!c.facet || !shown[i]) return;
            const vals = Array.from(new Set(items.map(function (it) { return it.values[i]; }).filter(Boolean)));
            if (vals.length < 2) return;
            vals.sort(function (a, b) { return a.localeCompare(b, 'bg', { numeric: true }); });
            const s = el('select', 'ep-facet');
            s.setAttribute('aria-label', c.label);
            s.appendChild(new Option(c.label + ': всички', ''));
            vals.forEach(function (v) { s.appendChild(new Option(v, v)); });
            facets.push({ el: s, test: function (row, v) { return row.dataset['f' + i] === v; } });
            bar.appendChild(s);
        });
        if (kind === 'material' && items.some(function (it) { return it.opt.dataset.stock !== undefined && it.opt.dataset.stock !== ''; })) {
            const s = el('select', 'ep-facet');
            s.setAttribute('aria-label', 'Наличност');
            s.appendChild(new Option('Наличност: всички', ''));
            s.appendChild(new Option('В наличност', 'in'));
            s.appendChild(new Option('Без наличност', 'out'));
            facets.push({ el: s, test: function (row, v) { return row.dataset.stockState === v; } });
            bar.appendChild(s);
        }
        dlg.appendChild(bar);

        actions.concat(none ? [none] : []).forEach(function (o) {
            const b = el('button', 'btn-secondary ep-action', o === none ? 'Без избор' : o.textContent.trim());
            b.type = 'button';
            b.addEventListener('click', function () { pick(o); });
            bar.appendChild(b);
        });

        // --- table ---
        const wrap = el('div', 'ep-table-wrap');
        const table = el('table', 'ep-table');
        const head = el('tr');
        head.appendChild(el('th', null, 'Наименование'));
        cols.forEach(function (c, i) { if (shown[i] && c.column !== false) head.appendChild(el('th', null, c.label)); });
        table.appendChild(el('thead')).appendChild(head);
        const tbody = table.appendChild(el('tbody'));
        const span = head.children.length;
        const rows = [], groupRows = [];
        let lastGroup = null, current = null;
        items.forEach(function (it) {
            if (it.group && it.group !== lastGroup) {
                const gr = el('tr', 'ep-group');
                const td = el('td', null, it.group);
                td.colSpan = span;
                gr.appendChild(td);
                gr._rows = [];
                tbody.appendChild(gr);
                groupRows.push(gr);
            }
            lastGroup = it.group;
            const tr = el('tr', 'ep-row');
            tr.tabIndex = -1;
            tr.appendChild(el('td', null, it.opt.textContent.trim()));
            cols.forEach(function (c, i) {
                tr.dataset['f' + i] = it.values[i];
                if (shown[i] && c.column !== false) tr.appendChild(el('td', null, it.values[i]));
            });
            const stock = parseFloat(it.opt.dataset.stock);
            tr.dataset.stockState = stock > 0 ? 'in' : 'out';
            tr._text = (it.opt.textContent + ' ' + it.values.join(' ')).toLowerCase();
            tr._opt = it.opt;
            if (it.opt.value === select.value) { tr.classList.add('ep-current'); current = tr; }
            tr.addEventListener('click', function () { pick(it.opt); });
            if (it.group) groupRows[groupRows.length - 1]._rows.push(tr);
            rows.push(tr);
            tbody.appendChild(tr);
        });
        const empty = el('p', 'ep-empty text-muted', 'Няма съвпадения.');
        wrap.appendChild(table);
        wrap.appendChild(empty);
        dlg.appendChild(wrap);

        // --- filtering ---
        const searchMatch = function (r) {
            const q = search.value.trim().toLowerCase();
            return !q || q.split(/\s+/).every(function (w) { return r._text.indexOf(w) !== -1; });
        };
        const update = window.cascadeFilters ? window.cascadeFilters(rows, facets, searchMatch) : function () {};
        let active = null;
        const setActive = function (r) {
            if (active) active.classList.remove('ep-active');
            active = r;
            if (r) { r.classList.add('ep-active'); r.scrollIntoView({ block: 'nearest' }); }
        };
        const visible = function () { return rows.filter(function (r) { return !r.hidden; }); };
        const apply = function () {
            update();
            rows.forEach(function (r) {
                r.hidden = !(searchMatch(r) && facets.every(function (f) { return !f.el.value || f.test(r, f.el.value); }));
            });
            groupRows.forEach(function (g) { g.hidden = g._rows.every(function (r) { return r.hidden; }); });
            const vis = visible();
            empty.hidden = vis.length > 0;
            if (active && active.hidden) setActive(null);
        };
        search.addEventListener('input', apply);
        facets.forEach(function (f) { f.el.addEventListener('change', apply); });

        dlg.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                if (e.target.tagName === 'SELECT') return;
                const vis = visible();
                if (!vis.length) return;
                e.preventDefault();
                const i = vis.indexOf(active);
                setActive(e.key === 'ArrowDown' ? vis[Math.min(vis.length - 1, i + 1)] : vis[Math.max(0, i - 1)]);
            } else if (e.key === 'Enter' && (e.target === search || e.target.classList.contains('ep-row'))) {
                e.preventDefault();
                const r = e.target.classList.contains('ep-row') ? e.target : (active || visible()[0]);
                if (r) pick(r._opt);
            }
        });
        dlg.addEventListener('close', function () { dlg.remove(); });

        apply();
        document.body.appendChild(dlg);
        dlg.showModal();
        if (current) { setActive(current); }
        search.focus();
    }

    // Delegated, so selects added later (dialogs, JS-built rows) work without an init step.
    document.addEventListener('mousedown', function (e) {
        if (e.button !== 0) return;
        const select = e.target.closest && e.target.closest('select[data-picker]');
        if (!kindOf(select)) return;
        e.preventDefault();
        open(select);
    }, true);
    document.addEventListener('keydown', function (e) {
        const select = e.target;
        if (!select || select.tagName !== 'SELECT' || !kindOf(select)) return;
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'F4' || (e.altKey && e.key === 'ArrowDown')) {
            e.preventDefault();
            open(select);
        }
    }, true);
    window.openEntityPicker = open;
})();
