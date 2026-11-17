// Printed documents (partials/print_doc.html, static/css/print_doc.css). Right before printing:
// 1. measures the document in print geometry (A4, the @page margins) - pages = body height / (page - header),
//    the header (<thead>) repeats on every page;
// 2. when the sign-off (.doc-signatures, never split) would be the only thing on the last page, scales the body
//    down (CSS zoom, not below MIN_ZOOM) so it stays with the content on one page less;
// 3. adds "Страница X от Y" in the page footer only when the document has more than one page.
// ponytail: the page count is an estimate from the screen layout in print width (rows that may not split can push
// a little more onto the next page) - SAFETY leaves room for that.
(function () {
    const PAGE_HEIGHT_MM = 297 - 12 - 14;   // A4 minus the @page top/bottom margins in print_doc.css
    const MIN_ZOOM = 0.7;
    const SAFETY = 0.97;
    const STYLE_ID = 'doc-page-numbers';

    function mmToPx(mm) {
        const probe = document.createElement('div');
        probe.style.cssText = 'position:absolute;visibility:hidden;height:' + mm + 'mm';
        document.body.appendChild(probe);
        const px = probe.getBoundingClientRect().height;
        probe.remove();
        return px;
    }

    function pageNumbers(on) {
        const old = document.getElementById(STYLE_ID);
        if (old) old.remove();
        if (!on) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = '@page { @bottom-right { content: "Страница " counter(page) " от " counter(pages);' +
            ' font-family: "PT Sans", Arial, sans-serif; font-size: 8pt; color: #6b6b6b; } }';
        document.head.appendChild(style);
    }

    function layout() {
        const body = document.querySelector('.doc-sheet .doc-body');
        if (!body) return;
        const head = document.querySelector('.doc-sheet .doc-frame > thead');
        const sign = body.querySelector('.doc-signatures');
        body.style.zoom = '';
        document.documentElement.classList.add('doc-measure');
        const usable = mmToPx(PAGE_HEIGHT_MM) - (head ? head.getBoundingClientRect().height : 0);
        const total = body.getBoundingClientRect().height;
        const signHeight = sign ? sign.getBoundingClientRect().height : 0;
        document.documentElement.classList.remove('doc-measure');

        const content = total - signHeight;
        const contentPages = Math.max(1, Math.ceil(content / usable));
        const room = contentPages * usable - content;          // left on the last content page
        let pages = room >= signHeight ? contentPages : contentPages + 1;
        if (pages > contentPages) {
            const zoom = usable * contentPages * SAFETY / total;
            if (zoom >= MIN_ZOOM) {
                body.style.zoom = zoom.toFixed(3);
                pages = contentPages;
            }
        }
        pageNumbers(pages > 1);
    }

    window.addEventListener('beforeprint', layout);
    window.addEventListener('afterprint', function () {
        const body = document.querySelector('.doc-sheet .doc-body');
        if (body) body.style.zoom = '';
    });
    window.printDocLayout = layout;   // for checking the layout without a print dialog
})();
