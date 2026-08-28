// Turns a <select class="material-picker"> into a nested Type -> Material ->
// Price-lot dropdown, so several price-lots of "the same" material (name +
// dims + type - see CLAUDE.md delivery-note task) collapse into one
// expandable entry instead of a long flat list of near-duplicates - same
// grouping idea as storage_materials.html, applied to every material picker
// in the app. Reads grouping data from data-name/data-w/data-h/data-t/
// data-type/data-id/data-brand/data-price attributes on each <option> (see
// partials/material_options.html) - an option without data-name (e.g. a
// "+ Нов материал..." trigger, or a blank placeholder) renders as a plain,
// ungrouped row in its original position instead.
//
// The real <select> stays in the DOM (visually hidden, not display:none, so
// native `required` constraint validation still works) and stays the single
// source of truth - every existing page's own JS that reads select.value or
// listens for 'change' keeps working untouched. Assigning select.value from
// other code (a quick-create-material modal appending a fresh <option> and
// selecting it, an edit page prefilling a value) is intercepted via a
// property override so the trigger button's label never goes stale, without
// requiring any of those call sites to know this widget exists.
(function () {
    function injectStyles() {
        if (document.getElementById('material-picker-styles')) return;
        const style = document.createElement('style');
        style.id = 'material-picker-styles';
        style.textContent = `
.mp-wrap { position: relative; }
.mp-trigger { width: 100%; text-align: left; background: var(--bg-input); border: 1px solid var(--border-color); color: var(--text-main); padding: 8px 10px; border-radius: var(--border-radius-sm); cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 8px; font: inherit; }
.mp-trigger:disabled { opacity: 0.6; cursor: not-allowed; }
.mp-trigger .mp-trigger-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mp-trigger .mp-trigger-arrow { opacity: 0.6; flex-shrink: 0; }
.mp-panel { position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 50; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: var(--border-radius-sm); box-shadow: 0 8px 24px rgba(0,0,0,0.35); max-height: 340px; display: flex; flex-direction: column; overflow: hidden; }
.mp-panel[hidden] { display: none; }
.mp-search { margin: 8px; padding: 6px 8px; background: var(--bg-input); border: 1px solid var(--border-color); color: var(--text-main); border-radius: var(--border-radius-sm); flex-shrink: 0; }
.mp-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding-bottom: 6px; }
.mp-section-label { padding: 6px 10px 2px; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); }
.mp-row { padding: 7px 10px; cursor: pointer; display: flex; align-items: center; gap: 6px; }
.mp-row[hidden] { display: none; }
.mp-row:hover, .mp-row.mp-active { background: var(--bg-input); }
.mp-group-row .mp-chevron { display: inline-block; width: 0.9em; transition: transform 0.15s ease; opacity: 0.7; flex-shrink: 0; }
.mp-group-row.expanded .mp-chevron { transform: rotate(90deg); }
.mp-group-row .mp-lot-count { margin-left: auto; font-size: 0.8em; opacity: 0.6; padding-left: 8px; }
.mp-leaf-row { padding-left: 38px; font-size: 0.92em; }
.mp-empty { padding: 10px; color: var(--text-muted); font-size: 0.9em; }
`;
        document.head.appendChild(style);
    }

    function dimsLabel(g) {
        return [g.w, g.h, g.t].filter(function (v) { return v; }).map(function (v) { return v + 'mm'; }).join(' × ');
    }

    function unitFor(type) {
        return (type === 'rods' || type === 'pipes' || type === 'profiles') ? 'm' : 'm²';
    }

    function leafLabel(opt) {
        const price = opt.dataset.price;
        const parts = [];
        if (opt.dataset.id) parts.push('#' + opt.dataset.id);
        if (price) parts.push(parseFloat(price).toFixed(2) + ' €/' + unitFor(opt.dataset.type));
        parts.push(opt.dataset.brand || '-');
        return parts.join(' · ');
    }

    function buildTree(select) {
        // Sections follow the <select>'s actual top-level child order (not
        // "all optgroups, then loose options") - a loose leading placeholder
        // option (e.g. "-- Изберете материал --") must stay first, not get
        // pushed to the bottom behind every optgroup.
        const sections = [];
        function addSection(label, optionEls) {
            const groups = [];
            const groupIndex = {};
            const plain = [];
            optionEls.forEach(function (opt) {
                if (!opt.dataset.name) {
                    plain.push({ kind: 'plain', opt: opt });
                    return;
                }
                const key = [opt.dataset.name, opt.dataset.w, opt.dataset.h, opt.dataset.t, opt.dataset.type].join('|');
                let group = groupIndex[key];
                if (!group) {
                    group = { kind: 'group', name: opt.dataset.name, w: opt.dataset.w, h: opt.dataset.h, t: opt.dataset.t, type: opt.dataset.type, opts: [] };
                    groupIndex[key] = group;
                    groups.push(group);
                }
                group.opts.push(opt);
            });
            sections.push({ label: label, items: groups.concat(plain).sort(function (a, b) {
                // preserve original <option> order across the merged groups/plain list
                const aFirst = a.kind === 'plain' ? a.opt : a.opts[0];
                const bFirst = b.kind === 'plain' ? b.opt : b.opts[0];
                return Array.prototype.indexOf.call(optionEls, aFirst) - Array.prototype.indexOf.call(optionEls, bFirst);
            }) });
        }
        let looseRun = [];
        function flushLooseRun() {
            if (looseRun.length) { addSection(null, looseRun); looseRun = []; }
        }
        Array.prototype.forEach.call(select.children, function (child) {
            if (child.tagName === 'OPTGROUP') {
                flushLooseRun();
                addSection(child.label, Array.prototype.slice.call(child.querySelectorAll('option')));
            } else if (child.tagName === 'OPTION') {
                looseRun.push(child);
            }
        });
        flushLooseRun();
        return sections;
    }

    function initPicker(select) {
        if (select.dataset.mpInit) return;
        select.dataset.mpInit = '1';

        select.style.cssText = 'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;';

        const wrap = document.createElement('div');
        wrap.className = 'mp-wrap';
        select.parentNode.insertBefore(wrap, select);
        wrap.appendChild(select);

        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'mp-trigger';
        trigger.innerHTML = '<span class="mp-trigger-label"></span><span class="mp-trigger-arrow">▾</span>';
        wrap.appendChild(trigger);

        const panel = document.createElement('div');
        panel.className = 'mp-panel';
        panel.hidden = true;
        panel.innerHTML = '<input type="text" class="mp-search" placeholder="Търсене..."><div class="mp-list"></div>';
        wrap.appendChild(panel);
        const searchInput = panel.querySelector('.mp-search');
        const list = panel.querySelector('.mp-list');

        function labelForCurrentValue() {
            const opt = select.options[select.selectedIndex];
            return opt ? opt.textContent : '';
        }

        function syncTrigger() {
            trigger.querySelector('.mp-trigger-label').textContent = labelForCurrentValue();
            trigger.disabled = select.disabled;
        }

        function selectOption(opt) {
            select.value = opt.value; // triggers the reactive setter below, which calls syncTrigger()
            select.dispatchEvent(new Event('change', { bubbles: true }));
            closePanel();
        }

        function renderList(filterText) {
            list.innerHTML = '';
            const q = (filterText || '').trim().toLowerCase();
            const sections = buildTree(select);
            let anyVisible = false;
            sections.forEach(function (section) {
                const rows = [];
                section.items.forEach(function (item) {
                    if (item.kind === 'plain') {
                        const text = item.opt.textContent;
                        if (q && text.toLowerCase().indexOf(q) === -1) return;
                        const row = document.createElement('div');
                        row.className = 'mp-row mp-plain-row';
                        row.textContent = text;
                        row.addEventListener('click', function () { selectOption(item.opt); });
                        rows.push(row);
                        return;
                    }
                    const groupText = (item.name + ' ' + dimsLabel(item)).toLowerCase();
                    const matchingOpts = q
                        ? item.opts.filter(function (o) { return o.textContent.toLowerCase().indexOf(q) !== -1 || groupText.indexOf(q) !== -1; })
                        : item.opts;
                    if (q && matchingOpts.length === 0) return;
                    if (item.opts.length === 1) {
                        const row = document.createElement('div');
                        row.className = 'mp-row';
                        row.textContent = item.opts[0].textContent;
                        row.addEventListener('click', function () { selectOption(item.opts[0]); });
                        rows.push(row);
                        return;
                    }
                    const groupRow = document.createElement('div');
                    groupRow.className = 'mp-row mp-group-row';
                    const expanded = !!q; // auto-expand groups when actively searching
                    if (expanded) groupRow.classList.add('expanded');
                    groupRow.innerHTML = '<span class="mp-chevron">▶</span><span>' +
                        item.name + (dimsLabel(item) ? ' (' + dimsLabel(item) + ')' : '') + '</span>' +
                        '<span class="mp-lot-count">' + item.opts.length + ' лота</span>';
                    rows.push(groupRow);
                    const leafRows = (q ? matchingOpts : item.opts).map(function (o) {
                        const leaf = document.createElement('div');
                        leaf.className = 'mp-row mp-leaf-row';
                        leaf.hidden = !expanded;
                        leaf.textContent = leafLabel(o);
                        leaf.addEventListener('click', function () { selectOption(o); });
                        rows.push(leaf);
                        return leaf;
                    });
                    groupRow.addEventListener('click', function () {
                        const nowExpanded = groupRow.classList.toggle('expanded');
                        leafRows.forEach(function (r) { r.hidden = !nowExpanded; });
                    });
                });
                if (!rows.length) return;
                anyVisible = true;
                if (section.label) {
                    const heading = document.createElement('div');
                    heading.className = 'mp-section-label';
                    heading.textContent = section.label;
                    list.appendChild(heading);
                }
                rows.forEach(function (r) { list.appendChild(r); });
            });
            if (!anyVisible) {
                const empty = document.createElement('div');
                empty.className = 'mp-empty';
                empty.textContent = 'Няма съвпадения.';
                list.appendChild(empty);
            }
        }

        const PANEL_MAX_HEIGHT = 340; // keep in sync with .mp-panel's max-height above
        const PANEL_MIN_WIDTH = 320; // wide enough for a price-lot leaf row even when the trigger itself is squeezed into a narrow form column

        // A trigger squeezed into a narrow form column (see admin_details.html's
        // grid layout) would otherwise force an equally narrow, hard-to-read
        // panel; a trigger near the bottom of the viewport would otherwise open
        // a panel that runs off-screen. Both are recomputed on every open since
        // the trigger's position can change between opens (window resize,
        // content above it changing height, etc.) - see the "space out the UI"/
        // "dynamic to avoid clipping" task.
        function positionPanel() {
            panel.style.left = '0';
            panel.style.right = 'auto';
            panel.style.top = 'calc(100% + 4px)';
            panel.style.bottom = '';
            panel.style.width = '';

            const triggerRect = trigger.getBoundingClientRect();
            const viewportW = document.documentElement.clientWidth;
            const viewportH = document.documentElement.clientHeight;
            const width = Math.min(Math.max(triggerRect.width, PANEL_MIN_WIDTH), viewportW - 16);
            panel.style.width = width + 'px';

            const overflowRight = (triggerRect.left + width) - viewportW + 8;
            if (overflowRight > 0) panel.style.left = (-overflowRight) + 'px';

            const spaceBelow = viewportH - triggerRect.bottom;
            const spaceAbove = triggerRect.top;
            const flipUp = spaceBelow < PANEL_MAX_HEIGHT + 8 && spaceAbove > spaceBelow;
            if (flipUp) {
                panel.style.top = 'auto';
                panel.style.bottom = 'calc(100% + 4px)';
            }
            // Neither side may fully fit the usual 340px cap on a short/cramped
            // viewport - clamp to whichever side was actually picked so the
            // panel scrolls internally instead of running off-screen.
            const available = (flipUp ? spaceAbove : spaceBelow) - 12;
            panel.style.maxHeight = Math.max(120, Math.min(PANEL_MAX_HEIGHT, available)) + 'px';
        }

        function openPanel() {
            if (select.disabled) return;
            searchInput.value = '';
            renderList('');
            panel.hidden = false;
            positionPanel();
            searchInput.focus();
        }
        function closePanel() { panel.hidden = true; }

        trigger.addEventListener('click', function () {
            if (panel.hidden) openPanel(); else closePanel();
        });
        searchInput.addEventListener('input', function () { renderList(searchInput.value); });
        document.addEventListener('click', function (e) {
            if (!wrap.contains(e.target)) closePanel();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !panel.hidden) closePanel();
        });

        // Make select.value reactive so any OTHER code that sets it directly
        // (quick-create-material appending+selecting a fresh option, a page
        // prefilling an edit form) keeps the trigger label in sync without
        // needing to know this widget exists - see file header.
        const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
        Object.defineProperty(select, 'value', {
            configurable: true,
            enumerable: true,
            get: function () { return desc.get.call(select); },
            set: function (v) { desc.set.call(select, v); syncTrigger(); },
        });
        select.addEventListener('change', syncTrigger);
        // A quick-create modal also just appends a fresh <option> - watch for
        // that so a stale tree isn't served next time the panel opens.
        new MutationObserver(function () { syncTrigger(); }).observe(select, { childList: true });

        syncTrigger();
    }

    function init() {
        injectStyles();
        document.querySelectorAll('select.material-picker').forEach(initPicker);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
