// Search + column filters for every list table (owner's decision 2026-11-19: "навсякъде където има списъци и
// таблици трябват филтри и търсене"). Loaded by partials/navbar.html BEFORE remember_filters.js: both run on
// DOMContentLoaded, and listeners fire in registration order, so the controls exist when remember_filters
// restores the saved values (and fires the input/change events this file listens to).
//
// Which tables: table.users-table / table.data-table with a <thead> and at least MIN_ROWS data rows. Skipped:
// inside a <dialog> or <form> or with number/text inputs in the rows (line editors), print documents (.doc-sheet/.doc-frame/.doc-table), tbody[data-tree],
// rows that already carry [data-search] (the page uses initTableSearch), a card that already has its own search
// box or filter select, and data-table-tools="off" on the table.
// Controls (inserted before the .table-responsive wrapper): search box #tt{n}Search over all cell text, one
// select #filter{n}c{col} per categorical column (2..15 distinct texts, fewer than the rows; not numbers, dates,
// money or action buttons), dependent via cascadeFilters, and "Изчисти". Tables with rowspan/colspan in the body
// get only the search box. Rows are hidden with the hidden attribute; rows the page itself hid are left alone.
(function () {
    var MIN_ROWS = 6, MAX_VALUES = 15;
    var NUMERIC = /^[-+−]?[\d\s.,]+\s*(€|лв\.?|eur|%|бр\.?|кг|kg|мм|mm|см|cm|m²|м²|м2|m2|м|m|ч\.?|h|мин\.?|kw|kwh|w|v|a)?$/i;
    var DATE = /^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|^\d{4}-\d{2}-\d{2}/;

    function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
    function firstLine(cell) {
        var lines = (cell.textContent || '').split('\n').map(function (l) { return l.trim(); }).filter(Boolean);
        return lines.length ? lines[0] : '';
    }
    function isAction(cell) {
        if (!cell.querySelector('button, form, a.btn, input, select')) return false;
        var c = cell.cloneNode(true);
        Array.prototype.forEach.call(c.querySelectorAll('button, form, a.btn, input, select'), function (e) { e.remove(); });
        return !c.textContent.trim();
    }

    function eligible(table) {
        if (table.getAttribute('data-table-tools') === 'off') return false;
        if (!table.tHead || !table.tBodies.length) return false;
        if (table.closest('dialog, form, .doc-sheet, .doc-frame, [data-search]')) return false;
        if (table.matches('.doc-table, .doc-frame') || table.querySelector('tbody[data-tree], tbody [data-search]')) return false;
        // rows with entry fields are an editor (production report quantities), not a list
        if (table.querySelector('tbody input[type="number"], tbody input[type="text"], tbody textarea')) return false;
        var anchor = table.closest('.table-responsive') || table;
        var card = anchor.closest('section, .admin-card, .card') || anchor.parentElement;
        if (card && card.querySelector('input[type="search"], input[id$="Search"], input[id$="SearchInput"], select[id^="filter"], select[id$="Filter"]')) return false;
        return true;
    }

    function dataRows(table) {
        var rows = [];
        Array.prototype.forEach.call(table.tBodies, function (tb) {
            Array.prototype.forEach.call(tb.rows, function (r) {
                if (r.hidden || r.classList.contains('search-empty-row') || r.querySelector('.no-data')) return;
                if (r.cells.length === 1 && r.cells[0].colSpan > 1) return;   // "Няма записи" message row
                rows.push(r);
            });
        });
        return rows;
    }

    function headers(table) {
        var hr = table.tHead.rows[table.tHead.rows.length - 1];
        return hr ? Array.prototype.map.call(hr.cells, function (c) { return c.textContent.replace(/\s+/g, ' ').trim(); }) : [];
    }

    function enhance(table, n) {
        var rows = dataRows(table);
        if (rows.length < MIN_ROWS) return;
        var heads = headers(table);
        var spans = rows.some(function (r) {
            return Array.prototype.some.call(r.cells, function (c) { return c.rowSpan > 1 || c.colSpan > 1; });
        });

        var bar = document.createElement('div');
        bar.className = 'tt-bar no-print';
        var search = document.createElement('input');
        search.type = 'text';
        search.id = 'tt' + n + 'Search';
        search.placeholder = 'Търсене...';
        bar.appendChild(search);

        var filters = [];
        if (!spans) heads.forEach(function (head, col) {
            if (!head) return;
            var cells = rows.map(function (r) { return r.cells[col]; });
            if (cells.some(function (c) { return !c; })) return;
            if (cells.filter(isAction).length * 2 > cells.length) return;
            var values = cells.map(firstLine).filter(Boolean);
            if (values.filter(function (v) { return NUMERIC.test(v) || DATE.test(v); }).length * 2 > values.length) return;
            var distinct = Array.from(new Set(values)).sort(function (a, b) { return a.localeCompare(b, 'bg'); });
            if (distinct.length < 2 || distinct.length > MAX_VALUES || distinct.length >= rows.length) return;
            var sel = document.createElement('select');
            sel.id = 'filter' + n + 'c' + col;
            var all = document.createElement('option');
            all.value = '';
            all.textContent = 'Всички: ' + head;
            sel.appendChild(all);
            distinct.forEach(function (v) {
                var o = document.createElement('option');
                o.value = v;
                o.textContent = v;
                sel.appendChild(o);
            });
            bar.appendChild(sel);
            filters.push({el: sel, test: function (row, v) { return row.cells[col] && firstLine(row.cells[col]) === v; }});
        });

        var clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'btn-secondary';
        clear.textContent = 'Изчисти';
        bar.appendChild(clear);

        var empty = document.createElement('tr');
        empty.className = 'tt-empty';
        empty.hidden = true;
        var td = document.createElement('td');
        td.colSpan = Math.max(heads.length, 1);
        td.textContent = 'Няма съвпадения.';
        empty.appendChild(td);
        table.tBodies[table.tBodies.length - 1].appendChild(empty);

        function textMatch(row) {
            var q = norm(search.value);
            return !q || norm(row.textContent).indexOf(q) !== -1;
        }
        var cascade = filters.length && window.cascadeFilters ? window.cascadeFilters(rows, filters, textMatch) : null;

        function apply() {
            if (cascade) cascade();
            var visible = 0;
            rows.forEach(function (r) {
                var ok = textMatch(r) && filters.every(function (f) { return !f.el.value || f.test(r, f.el.value); });
                r.hidden = !ok;
                if (ok) visible++;
            });
            empty.hidden = visible !== 0;
        }

        var timer = null;
        search.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(apply, 120); });
        filters.forEach(function (f) { f.el.addEventListener('change', apply); });
        clear.addEventListener('click', function () {
            search.value = '';
            filters.forEach(function (f) { f.el.value = ''; });
            apply();
            search.dispatchEvent(new Event('input', {bubbles: true}));   // remember_filters saves the cleared values
        });

        var anchor = table.closest('.table-responsive') || table;
        anchor.parentNode.insertBefore(bar, anchor);
        if (cascade) cascade();
    }

    function run() {
        // decide for every table first: an enhanced table's own search box must not disqualify its neighbour
        var picked = [];
        Array.prototype.forEach.call(document.querySelectorAll('table'), function (table, n) {
            if (table.matches('.users-table, .data-table') && eligible(table)) picked.push([table, n]);
        });
        picked.forEach(function (p) { enhance(p[0], p[1]); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
    else run();
})();
