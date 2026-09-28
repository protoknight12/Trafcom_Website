// Wiki-style edit: click the pencil to pop open a small edit window for that
// item (see edit_window.html). On save, that window reloads the page that
// opened it so the list stays in sync, matching Wikipedia's "edit in a
// separate view, save, and the article updates" flow.
function openEditWindow(url) {
    // Shared frame dialog (form_dialog.js, loaded by the navbar); plain
    // popup only on a page without it.
    if (window.openFrameDialog) window.openFrameDialog(url);
    else window.open(url, 'trafcom_edit', 'width=560,height=560');
}
