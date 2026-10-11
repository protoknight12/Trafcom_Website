// 3D view of a press brake and of a bend program: the cross-sections from bending.profile.step_frames (mm, x depth / y up) extruded along z (the bend line),
// inside a schematic machine built from the machine's real dimensions (PressBrakeConfig + the controller's parameters). Every element can be shown/hidden.
// ponytail: frames are linearly interpolated (no physics); frame, ram, bed ... are boxes at the real overall sizes (the catalogue gives no outline);
// tools are as wide as the part + margin; the part is boxes per centre-line segment. The fingers use the controller's real plan outlines.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const sim = JSON.parse(document.getElementById('simdata').textContent);
const M = sim.machine || {};
const view = document.getElementById('view');
const $ = id => document.getElementById(id);
if (!sim.steps.length) { view.textContent = 'Няма стъпки с инструменти за симулация.'; throw new Error('no steps'); }

const ELEMENTS = [   // key, label, group, shown by default
    ['part', 'Детайл', 'Процес', 1], ['punch', 'Поансон + закрепване', 'Процес', 1], ['die', 'Матрица', 'Процес', 1], ['fingers', 'Упори (нива)', 'Процес', 1],
    ['ram', 'Горна греда (Y1/Y2)', 'Машина', 1], ['clamps', 'Скоби на горния инструмент', 'Машина', 1], ['dieholder', 'Държач на матрицата', 'Машина', 1],
    ['crowning', 'Бомбиране', 'Машина', 1], ['bed', 'Долна маса', 'Машина', 1], ['frames', 'Странични рамки', 'Машина', 1],
    ['bg_beam', 'Заден упор: греда и колички', 'Заден упор', 1], ['bg_columns', 'Заден упор: колони R', 'Заден упор', 1], ['bg_rails', 'Заден упор: водачи X', 'Заден упор', 1],
    ['frontarms', 'Предни рамена', 'Околност', 1], ['panel', 'Пулт Delem', 'Околност', 1], ['floor', 'Под', 'Околност', 1],
];
const vis = {};
try { Object.assign(vis, JSON.parse(localStorage.getItem('bendSimVis') || '{}')); } catch (e) { /* private window */ }
ELEMENTS.forEach(([k, , , d]) => { if (!(k in vis)) vis[k] = !!d; });
const save = () => { try { localStorage.setItem('bendSimVis', JSON.stringify(vis)); } catch (e) { /* ignore */ } };

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(devicePixelRatio);
view.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14171a);
const camera = new THREE.PerspectiveCamera(40, 1, 1, 20000);
const controls = new OrbitControls(camera, renderer.domElement);
scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.1); sun.position.set(800, 1500, 1200); scene.add(sun);

const W = sim.width, TOOL_W = W + 20, T = sim.thickness;
const WL = M.work_length || 2050, BF = M.between_frames || 1730, WH = M.work_height || 1000, OV = M.overall || [WL + 550, 1450, 2650];
const mat = (c, o) => new THREE.MeshStandardMaterial({ color: c, metalness: 0.5, roughness: 0.45, transparent: o !== undefined, opacity: o === undefined ? 1 : o });
const mats = { die: mat(0x8fa0b0), punch: mat(0xcfd8e0), clamp: mat(0x7d8a97), finger: mat(0x4f9d69), part: mat(0xe26a4a), hit: mat(0xff3b30),
    frame: mat(0x9db4cc, 0.5), ram: mat(0x6f8299), bed: mat(0x4a5560), holder: mat(0x6b7886), crowning: mat(0xc9a227), rail: mat(0x9aa5b1), beam: mat(0x2f6f9f),
    arm: mat(0x7d8a97), panel: mat(0x22262b), screen: mat(0x2f8fbf), floor: mat(0x1d2126) };

const lineMat = new THREE.LineBasicMaterial({ color: 0xaab6c4 });
const staticG = new THREE.Group(), dynG = new THREE.Group();
scene.add(staticG, dynG);
const tag = (m, key) => { m.userData.key = key; m.visible = !!vis[key]; return m; };
const box = (key, m, sx, sy, sz, x, y, z, parent) => { const b = tag(new THREE.Mesh(new THREE.BoxGeometry(sx, sy, sz), m), key); b.position.set(x, y, z); (parent || staticG).add(b); return b; };
const shapeMesh = (pts, m, depth) => {
    const g = new THREE.ExtrudeGeometry(new THREE.Shape(pts.map(p => new THREE.Vector2(p[0], p[1]))), { depth, bevelEnabled: false });
    g.translate(0, 0, -depth / 2);
    return new THREE.Mesh(g, m);
};
const lerpPts = (a, b, f) => a.map((p, i) => [p[0] + (b[i][0] - p[0]) * f, p[1] + (b[i][1] - p[1]) * f]);

// ---- the fixed machine (built once) ----
(function buildMachine() {
    const top = OV[2] - WH;   // y of the top of the machine, the die's top plane is y = 0
    // side plates: the profile traced from the body drawing (the throat is open to the front so the tools run past the plates), x = a_die - a, y = b - b_die
    const F = M.frame || { a_die: 1190, b_die: 1010, thickness: 100, profile: [[0, 0], [1300, 0], [1300, 600], [1020, 600], [811, 932], [811, 1500], [960, 1650], [1200, 1650], [1200, 2650], [0, 2650]] };
    const plate = F.profile.map(([a, b]) => [F.a_die - a, b - F.b_die]);
    for (const s of [-1, 1]) {
        const f = tag(shapeMesh(plate, mats.frame, F.thickness), 'frames');
        f.position.z = s * (BF / 2 + F.thickness / 2);
        staticG.add(f);
        const e = tag(new THREE.LineSegments(new THREE.EdgesGeometry(f.geometry), lineMat), 'frames');
        e.position.copy(f.position);
        staticG.add(e);
    }
    const ax = a => F.a_die - a, by = b => b - F.b_die;
    box('frames', mats.frame, 100, 900, BF, ax(1250), by(450), 0);   // the lower front cross panel between the plates; its top flange (b = 900) carries the die holder
    box('frames', mats.frame, 120, 180, BF, ax(120), by(2480), 0);   // the upper rear tie tube
    box('frames', mats.frame, 120, 180, BF, ax(560), by(2380), 0);   // the upper tube behind the guide arms
    box('floor', mats.floor, 3600, 4, 3000, 150, -F.b_die - 2, 0);
    box('bed', mats.bed, 220, 300, WL, 0, -260, 0);   // rests on the plates' lower arms (b = 600), its top is the panel's flange
    box('crowning', mats.crowning, 232, 50, WL, 0, -140, 0);
    // die holder: the sections of the real holder (e.g. 550 + 1050 + 450)
    let z0 = -WL / 2;
    for (const len of (M.die_holder || [WL])) { box('dieholder', mats.holder, 120, 50, len - 2, 0, -85, z0 + len / 2); z0 += len; }
    // back gauge: X guides on both sides of the machine (the beam and the fingers move along them)
    const xr = (M.x_max || 650);
    for (const s of [-1, 1]) box('bg_rails', mats.rail, xr + 420, 40, 60, (xr + 420) / 2 + 60, -75, s * (BF / 2 - 90));
    // front sheet support arms
    for (const s of [-1, 1]) box('frontarms', mats.arm, 420, 20, 110, -330, -25, s * 420);
    // operator panel on a pole, front right
    box('panel', mats.panel, 60, 1400, 60, -380, -WH + 700, BF / 2 + 260);
    box('panel', mats.screen, 40, 260, 340, -380, 420, BF / 2 + 260);
})();

// ---- the program ----
let cur = 0, time = 0, playing = true, dynamic = [], stepStatic = [];
function loadStep(i) {
    cur = i; time = 0; $('step').value = i;
    stepStatic.forEach(m => { staticG.remove(m); m.geometry.dispose(); });
    const st = sim.steps[i];
    const die = tag(shapeMesh(st.die, mats.die, TOOL_W), 'die');
    staticG.add(die); stepStatic = [die];
    const hit = st.hits.length > 0;
    $('warn').style.display = hit || (M.warn || []).length ? 'block' : 'none';
    $('warn').textContent = [hit ? 'КОЛИЗИЯ: ' + st.hits.join('; ') : '', ...(M.warn || [])].filter(Boolean).join(' · ');
    $('label').textContent = `Стъпка ${st.seq}: ${st.punch} / ${st.die_code} V${st.v} · ${st.angle}°` + (M.finger_machine_z ? ` · Z1 ${M.finger_machine_z[0]} / Z2 ${M.finger_machine_z[1]}` : '');
    dynG.userData.hit = hit;
}

function fingerMeshes(poly, add) {
    const nose = Math.min(...poly.map(p => p[0])), base = Math.min(...poly.map(p => p[1]));
    (M.finger_z || [-60, 60]).forEach((zp, n) => {
        const data = M.finger && (n ? M.finger.z2 : M.finger.z1);
        if (!data) {   // no outline data: the cross-section as a block
            const g = shapeMesh(poly, mats.finger, 85); g.position.z = zp; add(tag(g, 'fingers')); return;
        }
        for (const lv of data.levels) {
            if (lv.outline.length < 3) continue;
            // plan outline (x = lateral, y = depth from the nose) -> scene x = depth, z = lateral; the level is extruded up by its height
            const shape = new THREE.Shape(lv.outline.map(p => new THREE.Vector2(p[1] + lv.offset[1], -p[0])));
            const g = new THREE.ExtrudeGeometry(shape, { depth: lv.height, bevelEnabled: false });
            g.rotateX(-Math.PI / 2);
            const m = tag(new THREE.Mesh(g, mats.finger), 'fingers');
            m.position.set(nose, base + lv.offset[2], zp);
            add(m);
        }
        // the R column behind the finger
        add(box('bg_columns', mats.beam, 50, 300, 70, nose + 470, base + 140, zp, dynG));
    });
    // the X beam along z, carried by the guides, moves with the finger's axis X
    add(box('bg_beam', mats.beam, 110, 160, 1450, nose + 520, base + 40, 0, dynG));
    for (const s of [-1, 1]) add(box('bg_beam', mats.beam, 130, 90, 90, nose + 520, base - 20, s * (BF / 2 - 90), dynG));
}

function draw(f) {
    dynamic.forEach(m => { dynG.remove(m); m.geometry.dispose(); });
    dynamic = [];
    const st = sim.steps[cur], fr = st.frames;
    const k = Math.min(Math.floor(f), fr.length - 1), a = fr[k], b = fr[Math.min(k + 1, fr.length - 1)], u = f - k;
    const add = m => { if (m.parent !== dynG) dynG.add(m); dynamic.push(m); return m; };
    const punch = lerpPts(a.punch, b.punch, u), clamp = lerpPts(a.clamp, b.clamp, u);
    add(tag(shapeMesh(punch, mats.punch, TOOL_W), 'punch'));
    add(tag(shapeMesh(clamp, mats.clamp, TOOL_W), 'punch'));
    // the upper beam and its tool clamps hang on the top of the punch's tang
    const top = Math.max(...clamp.map(p => p[1]));
    add(box('ram', mats.ram, 90, 420, WL, 0, top + 110 + 210, 0, dynG));   // the upper table is a tall flat plate; its ends run in the guides of the plates
    const n = M.clamps || 10, cl = WL / n;
    for (let i = 0; i < n; i++) add(box('clamps', mats.clamp, 190, 110, cl - 20, 0, top + 55, -WL / 2 + cl * (i + 0.5), dynG));
    fingerMeshes(lerpPts(a.finger, b.finger, u), add);
    const line = lerpPts(a.part, b.part, u);
    for (let i = 0; i < line.length - 1; i++) {
        const [x0, y0] = line[i], [x1, y1] = line[i + 1], L = Math.hypot(x1 - x0, y1 - y0);
        if (L < 1e-6) continue;
        const m = tag(new THREE.Mesh(new THREE.BoxGeometry(L + (i > 0 ? T * 0.15 : 0) + (i < line.length - 2 ? T * 0.15 : 0), T, W), dynG.userData.hit ? mats.hit : mats.part), 'part');
        m.position.set((x0 + x1) / 2, (y0 + y1) / 2, 0);   // free ends stay exactly on the centre-line end (the finger touches that wall)
        m.rotation.z = Math.atan2(y1 - y0, x1 - x0);
        add(m);
    }
}

// ---- UI ----
const panel = $('elements');
let lastGroup = '';
ELEMENTS.forEach(([key, label, group]) => {
    if (group !== lastGroup) { const h = document.createElement('div'); h.className = 'grp'; h.textContent = group; panel.appendChild(h); lastGroup = group; }
    const l = document.createElement('label');
    l.innerHTML = `<input type="checkbox" data-k="${key}"${vis[key] ? ' checked' : ''}> ${label}`;
    panel.appendChild(l);
});
const applyVis = () => {
    [staticG, dynG].forEach(g => g.traverse(o => { if (o.userData.key) o.visible = !!vis[o.userData.key]; }));
    panel.querySelectorAll('input').forEach(i => { i.checked = !!vis[i.dataset.k]; });
    save();
};
panel.addEventListener('change', e => { if (e.target.dataset.k) { vis[e.target.dataset.k] = e.target.checked; applyVis(); } });
const preset = fn => { ELEMENTS.forEach(([k]) => { vis[k] = fn(k); }); applyVis(); };
const PROCESS = ['part', 'punch', 'die', 'fingers'];
$('p_all').onclick = () => preset(() => true);
$('p_tools').onclick = () => preset(k => PROCESS.includes(k));
$('p_back').onclick = () => preset(k => PROCESS.includes(k) || k.startsWith('bg_'));
$('p_mach').onclick = () => preset(k => !PROCESS.includes(k) || k === 'die');
const cam = (pos, tgt) => { camera.position.set(...pos); controls.target.set(...tgt); controls.update(); };
const VIEWS = { tool: () => cam([-160, 120, 380], [0, 20, 0]), all: () => cam([-2600, 1200, 3200], [150, 300, 0]),
    front: () => cam([-3800, 250, 0], [150, 300, 0]), side: () => cam([150, 300, 4200], [150, 300, 0]), top: () => cam([150, 4600, 1], [150, 0, 0]) };
Object.keys(VIEWS).forEach(k => { $('v_' + k).onclick = VIEWS[k]; });
VIEWS.tool();

sim.steps.forEach((s, i) => $('step').add(new Option(`Стъпка ${s.seq} · ${s.angle}°`, i)));
$('step').onchange = e => loadStep(+e.target.value);
$('prev').onclick = () => loadStep((cur + sim.steps.length - 1) % sim.steps.length);
$('next').onclick = () => loadStep((cur + 1) % sim.steps.length);
$('play').onclick = () => { playing = !playing; $('play').innerHTML = playing ? '&#9208; Пауза' : '&#9654; Пусни'; };

const FPS = 4;   // frames of the stroke per second at 1x
let last = performance.now();
function resize() { const w = view.clientWidth, h = view.clientHeight; renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); }
addEventListener('resize', resize); resize();
loadStep(0);
(function loop(now) {
    const dt = (now - last) / 1000; last = now;
    const n = sim.steps[cur].frames.length - 1;
    if (playing) {
        time += dt * FPS * +$('speed').value;
        if (time > n + 3) loadStep((cur + 1) % sim.steps.length);   // a short pause at the end, then the next step
    }
    draw(Math.min(time, n));
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(loop);
})(last);
