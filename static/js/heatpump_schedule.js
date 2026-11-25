// Quick toggle of the heat pump's schedule: every [data-sched-toggle] button on the page (data-url, data-on="1|0").
// hpScheduleShow(on) updates them from a poll; a click posts enabled=1/0 (CSRF from the page's meta/hidden field).
(function () {
    function buttons() { return document.querySelectorAll('[data-sched-toggle]'); }
    function show(on) {
        buttons().forEach(function (b) {
            b.dataset.on = on ? '1' : '0';
            b.textContent = 'График: ' + (on ? 'вкл.' : 'изкл. (ръчно)');
            b.title = on ? 'Натисни, за да изключиш графика (ръчно управление)' : 'Натисни, за да включиш графика';
        });
    }
    window.hpScheduleShow = function (on) { if (on != null) show(!!on); };
    function csrf() {
        var el = document.querySelector('input[name="csrf_token"]') || document.querySelector('meta[name="csrf-token"]');
        return el ? (el.value || el.content) : '';
    }
    document.addEventListener('click', function (e) {
        var b = e.target.closest('[data-sched-toggle]');
        if (!b) return;
        var on = b.dataset.on !== '1';
        b.disabled = true;
        fetch(b.dataset.url, {method: 'POST', headers: {'X-CSRFToken': csrf(), 'Content-Type': 'application/x-www-form-urlencoded'},
                              body: new URLSearchParams({enabled: on ? '1' : '0'})})
            .then(function (r) { return r.json(); }).then(function (j) { if (j.ok) show(j.enabled); })
            .catch(function () {}).then(function () { b.disabled = false; });
    });
})();
