// Generic client-side filter for server-rendered admin/storage tables.
// Each <tr> carries a pre-lowercased data-search attribute (built once in
// Jinja) so filtering never re-reads/normalizes DOM text on every keystroke -
// each keypress is one indexOf() per row, debounced, no table rebuild.
// ponytail: plain DOM filtering, fine up to a few thousand rendered rows;
// past that, switch to server-side search + pagination instead of loading
// the whole catalog into one table.
function initTableSearch(inputId, tbodyId) {
    const input = document.getElementById(inputId);
    const tbody = document.getElementById(tbodyId);
    if (!input || !tbody) return;

    const rows = Array.from(tbody.querySelectorAll('tr[data-search]'));
    const emptyRow = tbody.querySelector('.search-empty-row');
    let debounceTimer = null;

    function applyFilter() {
        const q = input.value.trim().toLowerCase();
        let visible = 0;
        rows.forEach(function (row) {
            const match = !q || row.dataset.search.indexOf(q) !== -1;
            row.hidden = !match;
            if (match) visible++;
        });
        if (emptyRow) emptyRow.hidden = visible !== 0;
    }

    input.addEventListener('input', function () {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(applyFilter, 120);
    });
}
