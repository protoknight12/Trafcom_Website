// Camera add/edit form (admin_cameras.html and the edit window): the "Връзка" choice (NVR / ONVIF / RTSP) decides which fields show.
// With NVR the channels (name, IP, online - offline ones red) are read from the NVR; picking one fills the IP (and an empty name).
(function () {
    const form = Array.from(document.forms).find(f => f.elements.conn_type);
    if (!form) return;
    const E = form.elements, grp = n => E[n] && E[n].closest('.form-group');
    const SHOW = { nvr: ['nvr_id', 'channel', 'host'], onvif: ['host', 'port', 'username', 'password', 'snapshot_path'],
                   rtsp: ['host', 'port', 'rtsp_path', 'username', 'password', 'snapshot_path'] };
    const ALL = ['nvr_id', 'channel', 'host', 'port', 'rtsp_path', 'username', 'password', 'snapshot_path'];
    const sync = () => ALL.forEach(n => { const g = grp(n); if (g) g.style.display = SHOW[E.conn_type.value].includes(n) ? '' : 'none'; });
    let list = [];
    const note = document.createElement('p'); note.className = 'text-small text-muted'; note.style.marginTop = '4px';
    if (grp('channel')) grp('channel').append(note);
    async function load(keep) {
        if (!E.nvr_id.value) { E.channel.innerHTML = '<option value="">-- първо изберете NVR --</option>'; list = []; note.textContent = ''; return; }
        note.textContent = 'Чета каналите от NVR...';
        const r = await fetch('/admin/cameras/nvr/' + E.nvr_id.value + '/channels').catch(() => null), d = r ? await r.json().catch(() => ({})) : {};
        if (!r || !r.ok) { note.textContent = d.error || 'NVR не отговаря.'; return; }
        list = d; note.textContent = d.length + ' канала (офлайн - червени).';
        E.channel.innerHTML = '<option value="">--</option>';
        d.forEach(c => { const o = new Option(c.label, c.channel); if (!c.online) o.style.color = '#dc2626'; E.channel.append(o); });
        if (keep) E.channel.value = keep;
    }
    E.channel.addEventListener('change', () => {
        const c = list.find(x => String(x.channel) === E.channel.value);
        if (!c) return;
        E.host.value = c.ip;
        if (!E.name.value.trim()) E.name.value = c.name || ('Канал ' + c.channel);
    });
    E.nvr_id.addEventListener('change', () => load(''));
    E.conn_type.addEventListener('change', sync);
    sync();
    if (E.nvr_id.value) load(E.channel.value);     // edit: refresh the saved NVR's channels (colours, IPs) right away
})();
