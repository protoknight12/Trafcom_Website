// Generic client-side filter for server-rendered admin/storage lists.
// Each filterable item (a <tr>, or a card-like element such as a
// <section>) carries a pre-lowercased data-search attribute (built once in
// Jinja) so filtering never re-reads/normalizes DOM text on every keystroke -
// each keypress is one indexOf() per item, debounced, no rebuild.
// ponytail: plain DOM filtering, fine up to a few thousand rendered items;
// past that, switch to server-side search + pagination instead of loading
// everything into one page.
function initTableSearch(inputId, containerIds) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const ids = Array.isArray(containerIds) ? containerIds : [containerIds];
    const containers = ids.map(id => document.getElementById(id)).filter(Boolean);
    if (!containers.length) return;

    const rows = [];
    const emptyRows = [];
    containers.forEach(function (container) {
        rows.push(...container.querySelectorAll('[data-search]'));
        const emptyRow = container.querySelector('.search-empty-row');
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
