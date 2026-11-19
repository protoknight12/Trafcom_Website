// Dependent ("faceted") filter selects (owner's decision 2026-11-19): each select only offers the values that
// still exist among the rows matching every OTHER active filter - pick Вид "Стомана" and Марка / Дебелина list
// only what steel has. Works in every direction, not in a fixed order. The selected option always stays.
//
//   const update = cascadeFilters(rows, [
//       {el: selectType,  test: (row, v) => row.dataset.type === v},
//       {el: selectBrand, test: (row, v) => row.dataset.brand === v},
//   ], row => searchMatches(row));      // optional: filters that are not selects (search box, ...)
//   update();                            // call it again whenever a filter changes (the page's own apply())
//
// An option with value '' ("Всички ...") is never hidden. Options are hidden and disabled, not removed.
(function () {
    window.cascadeFilters = function (rows, filters, extra) {
        rows = Array.prototype.slice.call(rows);
        return function update() {
            filters.forEach(function (f) {
                const others = filters.filter(function (g) { return g !== f && g.el.value; });
                const live = rows.filter(function (r) {
                    return (!extra || extra(r)) && others.every(function (g) { return g.test(r, g.el.value); });
                });
                Array.prototype.forEach.call(f.el.options, function (o) {
                    if (!o.value) return;
                    const ok = o.value === f.el.value || live.some(function (r) { return f.test(r, o.value); });
                    o.hidden = !ok;
                    o.disabled = !ok;
                });
            });
        };
    };
})();
