// Keeps a list page's filters when you leave it and come back (owner's decision 2026-11-19): every search box
// and filter select is saved per page (path + element id) in localStorage and restored on the next visit,
// firing the same 'input'/'change' event the page listens to, so its own filter code re-runs. Loaded on
// every page by partials/navbar.html. Which elements: [data-remember], plus by naming convention search
// inputs (id ending in "Search"/"SearchInput") and filter selects (id starting with "filter" or ending in
// "Filter"); data-remember="off" opts one out. A page's "Изчисти" needs nothing: the cleared values are
// saved when the page is left. Only an element whose restored value differs is touched, so a select that
// navigates on change (?owner= on the storage page) only does so when the saved choice is another one; with
// data-url-param="owner" a URL that already carries ?owner= wins over the saved value.
(function () {
    var SELECTOR = '[data-remember], input[id$="Search"], input[id$="SearchInput"], select[id^="filter"], select[id$="Filter"]';

    function fields() {
        return Array.prototype.filter.call(document.querySelectorAll(SELECTOR), function (el) {
            return el.id && el.getAttribute('data-remember') !== 'off' && !el.closest('dialog');
        });
    }

    function key(el) { return 'filters:' + location.pathname + ':' + el.id; }

    function save() {
        fields().forEach(function (el) {
            try { localStorage.setItem(key(el), el.value); } catch (e) {}
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var changed = [];
        fields().forEach(function (el) {
            var saved = null;
            try { saved = localStorage.getItem(key(el)); } catch (e) {}
            if (saved === null || saved === el.value) return;
            // a filter the server applied from the URL (data-url-param="owner" + ?owner=...) keeps the link's choice
            var param = el.getAttribute('data-url-param');
            if (param && new URLSearchParams(location.search).has(param)) return;
            if (el.tagName === 'SELECT' && !Array.prototype.some.call(el.options, function (o) { return o.value === saved; })) return;
            el.value = saved;
            changed.push(el);
        });
        // all values first, then the events: a page's filter function reads every control at once
        changed.forEach(function (el) {
            el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
        });
        document.addEventListener('input', function (e) { if (e.target.matches && e.target.matches(SELECTOR)) save(); });
        document.addEventListener('change', function (e) { if (e.target.matches && e.target.matches(SELECTOR)) save(); });
        window.addEventListener('pagehide', save);
    });
})();
