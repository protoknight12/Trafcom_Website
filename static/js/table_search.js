// Generic client-side filter for server-rendered admin/storage tables.
// Each <tr> carries a pre-lowercased data-search attribute (built once in
// Jinja) so filtering never re-reads/normalizes DOM text on every keystroke -
// each keypress is one indexOf() per row, debounced, no table rebuild.
// ponytail: plain DOM filtering, fine up to a few thousand rendered rows;
// past that, switch to server-side search + pagination instead of loading
// the whole catalog into one table.
function initTableSearch(inputId, tbodyIds) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const ids = Array.isArray(tbodyIds) ? tbodyIds : [tbodyIds];
    const tbodies = ids.map(id => document.getElementById(id)).filter(Boolean);
    if (!tbodies.length) return;

    const rows = [];
    const emptyRows = [];
    tbodies.forEach(function (tbody) {
        rows.push(...tbody.querySelectorAll('tr[data-search]'));
        const emptyRow = tbody.querySelector('.search-empty-row');
        if (emptyRow) emptyRows.push(emptyRow);
    });
    let debounceTimer = null;

    function applyFilter() {
        const q = input.value.trim().toLowerCase();
        let visible = 0;
        rows.forEach(function (row) {
            const match = !q || row.dataset.search.indexOf(q) !== -1;
            row.hidden = !match;
            if (match) visible++;
        });
        emptyRows.forEach(function (emptyRow) { emptyRow.hidden = visible !== 0; });
    }

    input.addEventListener('input', function () {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(applyFilter, 120);
    });
}
