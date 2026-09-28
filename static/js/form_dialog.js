// Shared modal ("отделен фрейм") for every add/edit form in the app, loaded
// from partials/navbar.html. Three entry points:
//
//  1. <section data-dialog-section="+ Нов клиент"> - an inline "add new"
//     section (or a bare <form>, titled via data-dialog-heading) is moved
//     into a <dialog> on load and replaced by a button that opens it. One
//     attribute per page, the form itself is untouched.
//  2. <button data-dialog="dialogId" [data-action="url" data-values='{json}'
//     data-title="..."]> - opens an on-page <dialog class="form-dialog">.
//     With data-values the dialog's form switches to edit mode (action +
//     fields filled by input name); without, it's reset back to "add".
//  3. <a data-frame href="editUrl"> / openFrameDialog(url) - loads an
//     existing edit page (edit_*_window, .../edit) in an <iframe> inside a
//     dialog (data-frame="stay" for multi-page flows, data-frame-scope="/path/"
//     for flows that may move between pages under one prefix); the framed page hides
//     its navbar (html.embedded). Once the
//     framed page was submitted, closing the dialog reloads this page and
//     carries the framed page's flash messages over (sessionStorage), so
//     the list shows the change and its confirmation.
//  4. <select data-create="{{ create_url('material') }}"> - gets a "+" button
//     that opens that entity's list page in a frame with its "+ Нов ..."
//     dialog already open (?dialog=new:N clicks the N-th [data-new-button]).
//     After each save inside the frame this page is re-fetched, and any option
//     the fresh copy of the same <select> (matched by id) has that the live
//     one doesn't is added and selected - the half-filled form around it is
//     kept. Pages whose JS keeps its own copy of the catalog (price lists
//     etc.) listen for the 'create-new' event (detail.doc = fetched page).
(function () {
    const embedded = window.self !== window.top;
    if (embedded) document.documentElement.classList.add('embedded');

    const FLASH_KEY = 'frameDialogFlashes';

    function addCloseButton(dlg) {
        if (dlg.querySelector('.dialog-close, [class*="modal-close"]')) return;
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'dialog-close';
        x.setAttribute('data-dialog-close', '');
        x.setAttribute('aria-label', 'Затвори');
        x.textContent = '×';
        dlg.prepend(x);
    }

    // --- 2. on-page add/edit dialogs --------------------------------------
    function fillForm(form, values) {
        Object.keys(values).forEach(function (name) {
            const field = form.elements[name];
            if (!field) return;
            const els = (field instanceof RadioNodeList) ? Array.from(field) : [field];
            const v = values[name];
            const asList = Array.isArray(v) ? v.map(String) : null;
            els.forEach(function (el) {
                if (el.type === 'file') return;
                if (el.type === 'hidden' && el.dataset.origValue === undefined) el.dataset.origValue = el.value;
                if (el.type === 'checkbox') el.checked = asList ? asList.includes(el.value) : (!!v && v !== '0');
                else if (el.type === 'radio') el.checked = String(v) === el.value;
                else if (el.multiple && asList) Array.from(el.options).forEach(function (o) { o.selected = asList.includes(o.value); });
                else el.value = v == null ? '' : v;
            });
        });
    }

    function remember(el, attr, value) {
        if (el && el.dataset[attr] === undefined) el.dataset[attr] = value;
        return el ? el.dataset[attr] : null;
    }

    function openFormDialog(btn) {
        const dlg = document.getElementById(btn.dataset.dialog);
        if (!dlg) return;
        const form = dlg.querySelector('form');
        const values = btn.dataset.values ? JSON.parse(btn.dataset.values) : null;
        if (form) {
            const addAction = remember(form, 'addAction', form.getAttribute('action') || '');
            form.reset();
            form.querySelectorAll('input[type=hidden][data-orig-value]').forEach(function (el) { el.value = el.dataset.origValue; });
            form.setAttribute('action', btn.dataset.action || addAction);
            if (values) fillForm(form, values);
            // Let the page's own onchange handlers (dependent fields shown
            // per type/mode etc.) react to the filled/reset values.
            form.querySelectorAll('select, input[type=checkbox], input[type=radio]').forEach(function (el) {
                el.dispatchEvent(new Event('change', { bubbles: true }));
            });
        }
        const title = dlg.querySelector('[data-dialog-title]');
        if (title) {
            const addTitle = remember(title, 'addTitle', title.textContent);
            title.textContent = btn.dataset.title || addTitle;
        }
        const submit = dlg.querySelector('[data-dialog-submit]');
        if (submit) {
            const addLabel = remember(submit, 'addLabel', submit.textContent);
            submit.textContent = values ? (submit.dataset.editLabel || 'Запази') : addLabel;
        }
        // Pages with type-dependent fields etc. hook in here.
        dlg.dispatchEvent(new CustomEvent('dialog-open', { bubbles: true, detail: { values: values, button: btn } }));
        dlg.showModal();
    }
    window.openFormDialog = openFormDialog;

    // --- 3. iframe dialog for existing edit pages -------------------------
    let frameDlg = null, frame = null, framePath = null, frameLoads = 0, frameStay = false, frameScope = null, frameHooks = null;

    function flashesFrom(doc) {
        try {
            return Array.from(doc.querySelectorAll('.flash-messages .alert')).map(function (a) { return a.outerHTML; });
        } catch (e) { return []; }
    }

    // targetUrl: where the framed flow ended up (e.g. "create order" lands
    // on the production report) - go there instead of reloading this page,
    // unless it's this same page anyway.
    function finishFrame(extraMessage, targetUrl) {
        const flashes = frame ? flashesFrom(frame.contentDocument) : [];
        if (extraMessage && !flashes.length) flashes.push('<div class="alert alert-success">' + extraMessage + '</div>');
        try { sessionStorage.setItem(FLASH_KEY, JSON.stringify(flashes)); } catch (e) { /* storage blocked - just reload */ }
        if (targetUrl && new URL(targetUrl).pathname !== window.location.pathname) window.location.href = targetUrl;
        else window.location.reload();
    }

    function ensureFrameDialog() {
        if (frameDlg) return;
        frameDlg = document.createElement('dialog');
        frameDlg.className = 'form-dialog frame-dialog';
        frame = document.createElement('iframe');
        frame.title = 'Редакция';
        frameDlg.appendChild(frame);
        addCloseButton(frameDlg);
        document.body.appendChild(frameDlg);
        frame.addEventListener('load', function () {
            // the about:blank reset after closing is not part of the flow
            if (!frameDlg.open) return;
            frameLoads++;
            if (frameLoads === 1) return;
            if (frameHooks) { frameHooks.onLoad(); return; }
            let path = null, href = null;
            try { path = frame.contentWindow.location.pathname; href = frame.contentWindow.location.href; } catch (e) { /* cross-origin - treat as done */ }
            if (frameScope) {
                // Any page under the scope is still part of the flow (new
                // offer -> its edit page); leaving it ends the flow.
                if (!path || !path.startsWith(frameScope)) finishFrame(null, href);
                return;
            }
            if (frameStay) return;
            // Left the edit page (typical "save -> redirect to the list")
            // means the edit is done.
            if (path !== framePath) finishFrame(null, href);
        });
        frameDlg.addEventListener('close', function () {
            if (frameHooks) {
                const hooks = frameHooks;
                frameHooks = null;
                if (frameLoads > 1) hooks.onClose();
                frame.src = 'about:blank';
                return;
            }
            // Any second load means something was submitted - reload so the
            // list reflects it.
            if (frameLoads > 1) finishFrame();
            else frame.src = 'about:blank';
        });
    }

    // stay=true: a multi-page framed flow (e.g. a stack's battery list ->
    // edit a battery -> back) - navigating inside it doesn't end the edit,
    // only closing the dialog does.
    // scope: a path prefix - the flow may move between pages under it
    // (stays open), and ends once the framed page leaves it.
    // hooks: {onLoad, onClose} take over from the reload-this-page default
    // (used by the "+" create buttons, which must keep this page as it is).
    window.openFrameDialog = function (url, stay, scope, hooks) {
        ensureFrameDialog();
        frameStay = !!stay;
        frameScope = scope || null;
        frameHooks = hooks || null;
        framePath = new URL(url, window.location.href).pathname;
        frameLoads = 0;
        frame.src = url;
        frameDlg.showModal();
    };

    // Called by a framed edit_window.html once it saved, or by its
    // "Затвори" button.
    window.closeFrameDialog = function (saved) {
        if (saved) finishFrame('Запазено успешно.');
        else if (frameDlg) { frameLoads = 0; frameDlg.close(); }
    };

    // --- 4. "+" create-new next to a <select> -----------------------------
    function pullNewOptions(select) {
        return fetch(window.location.href, { credentials: 'same-origin' })
            .then(function (r) { return r.text(); })
            .then(function (html) {
                const doc = new DOMParser().parseFromString(html, 'text/html');
                const fresh = doc.getElementById(select.id);
                if (!fresh) return null;
                const have = new Set(Array.from(select.options).map(function (o) { return o.value; }));
                const added = Array.from(fresh.querySelectorAll('option')).filter(function (o) { return o.value && !have.has(o.value); });
                if (!added.length) return null;
                added.forEach(function (o) {
                    let target = select;
                    const group = o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : null;
                    if (group) {
                        target = Array.from(select.querySelectorAll('optgroup')).find(function (g) { return g.label === group; });
                        if (!target) {
                            target = document.createElement('optgroup');
                            target.label = group;
                            select.appendChild(target);
                        }
                    }
                    target.appendChild(document.importNode(o, true));
                });
                const value = added[added.length - 1].value;
                select.value = value;
                // catalog arrays first (mergeCatalog), so 'change' handlers
                // that look the new id up in them already find it
                select.dispatchEvent(new CustomEvent('create-new', { bubbles: true, detail: { doc: doc, value: value } }));
                select.dispatchEvent(new Event('change', { bubbles: true }));
                return value;
            })
            .catch(function () { return null; });
    }

    // For pages whose JS keeps its own copy of a catalog
    // (`const PRODUCTS = {{ products|tojson }};`): pulls the same array out of
    // the re-fetched page and appends the entries this page doesn't have yet.
    window.mergeCatalog = function (doc, name, target, key) {
        key = key || 'id';
        const re = new RegExp('const ' + name + ' = (.*);$', 'm');
        for (const script of doc.querySelectorAll('script:not([src])')) {
            const m = re.exec(script.textContent);
            if (!m) continue;
            let fresh;
            try { fresh = JSON.parse(m[1]); } catch (e) { return; }
            const have = new Set(target.map(function (x) { return String(x[key]); }));
            fresh.forEach(function (x) { if (!have.has(String(x[key]))) target.push(x); });
            return;
        }
    };

    function openCreateNew(select) {
        window.openFrameDialog(select.dataset.create, true, null, {
            // after each save in the frame: done as soon as the new record shows up here
            onLoad: function () {
                pullNewOptions(select).then(function (value) {
                    if (value !== null && frameDlg.open) frameDlg.close();
                });
            },
            onClose: function () { pullNewOptions(select); },
        });
    }

    function addCreateButton(select) {
        if (!select.id || select.dataset.createInit) return;
        select.dataset.createInit = '1';
        const row = document.createElement('div');
        row.className = 'select-create-row';
        select.parentNode.insertBefore(row, select);
        row.appendChild(select);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-create-new';
        btn.textContent = '+';
        btn.title = 'Добави нов';
        btn.setAttribute('aria-label', 'Добави нов');
        btn.addEventListener('click', function () { openCreateNew(select); });
        row.appendChild(btn);
    }

    // --- wiring -----------------------------------------------------------
    document.addEventListener('click', function (e) {
        const closer = e.target.closest('[data-dialog-close]');
        if (closer) { closer.closest('dialog').close(); return; }
        const opener = e.target.closest('[data-dialog]');
        if (opener) { e.preventDefault(); openFormDialog(opener); return; }
        const link = e.target.closest('a[data-frame]');
        if (link && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); window.openFrameDialog(link.href, link.dataset.frame === 'stay', link.dataset.frameScope); }
    });

    document.addEventListener('DOMContentLoaded', function () {
        // 1. inline "add" sections -> dialog + opener button
        document.querySelectorAll('[data-dialog-section]').forEach(function (section, i) {
            const dlg = document.createElement('dialog');
            dlg.className = 'form-dialog';
            dlg.id = section.id ? section.id + '-dialog' : 'section-dialog-' + i;
            const bar = document.createElement('div');
            bar.className = 'dialog-open-bar';
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn-submit';
            btn.textContent = section.dataset.dialogSection;
            btn.setAttribute('data-dialog', dlg.id);
            btn.setAttribute('data-new-button', '');
            bar.appendChild(btn);
            section.parentNode.insertBefore(bar, section);
            section.parentNode.insertBefore(dlg, section);
            dlg.appendChild(section);
            let heading = section.querySelector('h2, h3');
            if (!heading && section.dataset.dialogHeading) {
                heading = document.createElement('h3');
                heading.textContent = section.dataset.dialogHeading;
                dlg.prepend(heading);
            }
            if (heading) heading.setAttribute('data-dialog-title', '');
        });
        document.querySelectorAll('dialog.form-dialog').forEach(addCloseButton);

        document.querySelectorAll('select[data-create]:not([data-create=""])').forEach(addCreateButton);

        // ?dialog=new:N - opened by a "+" create button: start with the page's
        // N-th "+ Нов ..." dialog already open. After the page's own
        // DOMContentLoaded work (pickers etc.).
        const want = new URLSearchParams(window.location.search).get('dialog');
        const m = want && /^new(?::(\d+))?$/.exec(want);
        if (m) {
            setTimeout(function () {
                const btn = document.querySelectorAll('[data-new-button]')[(parseInt(m[1] || '1', 10)) - 1];
                if (btn) btn.click();
            }, 0);
        }

        if (!embedded) {
            let flashes = null;
            try { flashes = JSON.parse(sessionStorage.getItem(FLASH_KEY) || 'null'); sessionStorage.removeItem(FLASH_KEY); } catch (e) { /* ignore */ }
            if (flashes && flashes.length) {
                const box = document.createElement('div');
                box.className = 'flash-messages';
                box.innerHTML = flashes.join('');
                const main = document.querySelector('main') || document.body;
                main.prepend(box);
            }
        }
    });
})();
