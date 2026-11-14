// "Показвай цените" toggle for browser-print documents. Price cells/columns/rows carry class="price";
// body.no-prices hides them (CSS rule lives in each template). Choice is per document type
// (data-print-prices="<key>") in localStorage; ?prices=0 / ?prices=1 in the URL overrides it for that visit.
(function () {
    var cb = document.querySelector('input[data-print-prices]');
    if (!cb) return;
    var key = 'printPrices:' + cb.getAttribute('data-print-prices');
    var on = true;
    try { if (localStorage.getItem(key) === '0') on = false; } catch (e) {}
    if (/[?&]prices=0(&|$)/.test(location.search)) on = false;
    else if (/[?&]prices=1(&|$)/.test(location.search)) on = true;
    function apply() { document.body.classList.toggle('no-prices', !cb.checked); }
    cb.checked = on;
    apply();
    cb.addEventListener('change', function () {
        apply();
        try { localStorage.setItem(key, cb.checked ? '1' : '0'); } catch (e) {}
    });
})();
