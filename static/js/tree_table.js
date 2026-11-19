// Collapsible tree inside a <tbody data-tree>.
// Rows: data-node="id" (optional) + data-parent="parentId". A header row has class "tree-head" and a
// .tree-chevron (▶, rotated when open) in it; every other row is a leaf. Header cells may hold
// [data-count] (visible leaves) and [data-sum="kg"] (sum of the visible leaves' data-kg; optional
// data-dec decimals, data-suffix text). Closed header hides its whole subtree; opening restores the
// children to their own remembered state. Default: top level open, deeper closed. State per page in
// localStorage (ignored if blocked). Text only via textContent.
(function () {
    const trees = [];

    function load(key) { try { return JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { return {}; } }
    function save(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* storage blocked */ } }

    function build(tbody) {
        if (tbody._tree) return tbody._tree;
        const t = { tbody: tbody, rows: [], byId: {}, heads: [], saved: {},
                    key: 'tree:' + location.pathname + ':' + (tbody.id || 'x'), active: false };
        t.saved = load(t.key);
        Array.from(tbody.children).forEach(function (r) {
            if (!r.dataset.node && !r.dataset.parent) return;   // e.g. the "no matches" row
            const n = { el: r, id: r.dataset.node || null, parentId: r.dataset.parent || null,
                        head: r.classList.contains('tree-head'), kids: [], filtered: false, parent: null, depth: 0 };
            t.rows.push(n);
            if (n.id) t.byId[n.id] = n;
        });
        t.rows.forEach(function (n) {
            n.parent = n.parentId ? t.byId[n.parentId] || null : null;
            if (n.parent) n.parent.kids.push(n);
            for (let p = n.parent; p; p = p.parent) n.depth++;
            if (n.head) {
                t.heads.push(n);
                n.open = n.id in t.saved ? !!t.saved[n.id] : n.depth === 0;
            }
        });
        tbody._tree = t;
        trees.push(t);
        return t;
    }

    function leaves(n, out) {
        n.kids.forEach(function (k) { if (k.head) leaves(k, out); else out.push(k); });
        return out;
    }

    function render(t) {
        const empty = t.tbody.querySelector('.search-empty-row');
        let anyLeaf = false;
        t.rows.forEach(function (n) {
            n.vis = false; n.eff = false;
        });
        t.heads.forEach(function (h) {
            const ls = leaves(h, []).filter(function (l) { return !l.filtered; });
            h.live = ls;
            h.eff = h.open || (t.active && ls.length > 0);
        });
        t.rows.forEach(function (n) {
            let show = n.head ? n.live.length > 0 : !n.filtered;
            if (n.head && !t.active && n.live.length === 0 && leaves(n, []).length === 0) show = true; // empty group stays
            for (let p = n.parent; show && p; p = p.parent) if (!p.eff) show = false;
            n.el.hidden = !show;
            if (!n.head && !n.filtered) anyLeaf = true;
            if (n.head) {
                n.el.classList.toggle('open', n.eff);
                n.el.querySelectorAll('[data-count]').forEach(function (c) { c.textContent = n.live.length; });
                n.el.querySelectorAll('[data-sum]').forEach(function (c) {
                    let s = 0;
                    n.live.forEach(function (l) { s += parseFloat(l.el.dataset[c.dataset.sum]) || 0; });
                    c.textContent = s.toFixed(+c.dataset.dec || 0) + (c.dataset.suffix || '');
                });
            }
        });
        if (empty) empty.hidden = anyLeaf;
    }

    function persist(t) {
        const o = {};
        t.heads.forEach(function (h) { o[h.id] = h.open ? 1 : 0; });
        save(t.key, o);
    }

    // Hide leaves failing predicate(row); active=true auto-opens headers that hold matches.
    window.treeFilter = function (tbody, predicate, active) {
        const t = build(tbody);
        t.active = !!active;
        t.rows.forEach(function (n) { if (!n.head) n.filtered = !predicate(n.el); });
        render(t);
    };

    window.treeSetAll = function (open) {
        trees.forEach(function (t) {
            t.heads.forEach(function (h) { h.open = open; });
            persist(t); render(t);
        });
        document.querySelectorAll('[data-tree-section]').forEach(function (h) { setSection(h, !open); });
    };

    // Whole-block collapse (e.g. a type heading over its own table): <h4 data-tree-section="boxId">.
    function setSection(h, collapsed) {
        const box = document.getElementById(h.dataset.treeSection);
        if (box) box.classList.toggle('tree-collapsed', collapsed);
        h.classList.toggle('tree-section-closed', collapsed);
        const all = load('tree-sections:' + location.pathname);
        all[h.dataset.treeSection] = collapsed ? 1 : 0;
        save('tree-sections:' + location.pathname, all);
    }

    window.initTrees = function () {
        document.querySelectorAll('tbody[data-tree]').forEach(function (tb) {
            const t = build(tb);
            tb.addEventListener('click', function (e) {
                if (e.target.closest('a, button, input, select, form')) return;
                const row = e.target.closest('.tree-head');
                if (!row) return;
                const n = t.byId[row.dataset.node];
                if (!n) return;
                n.open = !n.eff;
                persist(t); render(t);
            });
            render(t);
        });
        const secs = load('tree-sections:' + location.pathname);
        document.querySelectorAll('[data-tree-section]').forEach(function (h) {
            h.addEventListener('click', function () { setSection(h, !h.classList.contains('tree-section-closed')); });
            const box = document.getElementById(h.dataset.treeSection);
            if (secs[h.dataset.treeSection] && box) { box.classList.add('tree-collapsed'); h.classList.add('tree-section-closed'); }
        });
        document.querySelectorAll('[data-tree-all]').forEach(function (b) {
            b.addEventListener('click', function () { window.treeSetAll(b.dataset.treeAll === 'expand'); });
        });
    };
})();
