// Wiki-style inline edit: click the pencil to swap a text view for its edit
// form in place, click cancel to swap back. Each pair of elements shares one
// id suffix: #view-<id> and #edit-<id>.
function toggleInlineEdit(id) {
    var view = document.getElementById('view-' + id);
    var edit = document.getElementById('edit-' + id);
    var editing = edit.style.display !== 'none';
    edit.style.display = editing ? 'none' : '';
    view.style.display = editing ? '' : 'none';
    if (!editing) {
        var firstInput = edit.querySelector('input, textarea');
        if (firstInput) firstInput.focus();
    }
}
