// 3D simulation of a bend program: the cross-sections from bending.profile.step_frames (mm, x/y) extruded along z (the bend line).
// ponytail: frames are linearly interpolated (no physics), tools are as wide as the part + margin, the part is boxes per centre-line segment.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const sim = JSON.parse(document.getElementById('simdata').textContent);
const view = document.getElementById('view');
const $ = id => document.getElementById(id);
if (!sim.steps.length) { view.textContent = 'Няма стъпки с инструменти за симулация.'; throw new Error('no steps'); }

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(devicePixelRatio);
view.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14171a);
const camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);
camera.position.set(-160, 120, 380);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 20, 0);
scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.1); sun.position.set(200, 400, 300); scene.add(sun);

const W = sim.width, N_F = sim.fingers || 2, FINGER_Z = Array.from({ length: N_F }, (_, i) => N_F === 1 ? 0 : (i / (N_F - 1) - 0.5) * 2 * Math.max(15, Math.min(W * 0.3, W / 2 - 15))), TOOL_W = W + 20, T = sim.thickness;
const mat = c => new THREE.MeshStandardMaterial({ color: c, metalness: 0.5, roughness: 0.45 });
const mats = { die: mat(0x8fa0b0), punch: mat(0xcfd8e0), clamp: mat(0x7d8a97), finger: mat(0x4f9d69), part: mat(0xe26a4a), hit: mat(0xff3b30) };
const group = new THREE.Group(); scene.add(group);
const shapeMesh = (pts, m, depth) => {
    const g = new THREE.ExtrudeGeometry(new THREE.Shape(pts.map(p => new THREE.Vector2(p[0], p[1]))), { depth, bevelEnabled: false });
    g.translate(0, 0, -depth / 2);
    return new THREE.Mesh(g, m);
};
const lerpPts = (a, b, f) => a.map((p, i) => [p[0] + (b[i][0] - p[0]) * f, p[1] + (b[i][1] - p[1]) * f]);

let cur = 0, time = 0, playing = true, parts = [], dynamic = [];
function loadStep(i) {
    cur = i; time = 0; $('step').value = i;
    group.clear();
    const st = sim.steps[i];
    group.add(shapeMesh(st.die, mats.die, TOOL_W));
    const hit = st.hits.length > 0;
    $('warn').style.display = hit ? 'block' : 'none';
    $('warn').textContent = hit ? 'КОЛИЗИЯ: ' + st.hits.join('; ') : '';
    $('label').textContent = `Стъпка ${st.seq}: ${st.punch} / ${st.die} V${st.v} · ${st.angle}°`;
    parts = [];
    dynamic = [];
    // meshes are rebuilt for every frame, so only the current one lives in the scene
    group.userData.hit = hit;
}
function draw(f) {
    dynamic.forEach(m => { group.remove(m); m.geometry.dispose(); });
    dynamic = [];
    const st = sim.steps[cur], fr = st.frames;
    const k = Math.min(Math.floor(f), fr.length - 1), a = fr[k], b = fr[Math.min(k + 1, fr.length - 1)], u = f - k;
    const add = m => { group.add(m); dynamic.push(m); };
    for (const key of ['punch', 'clamp']) add(shapeMesh(lerpPts(a[key], b[key], u), mats[key], TOOL_W));
    for (const z of FINGER_Z) {   // back-gauge fingers (two per machine on the Durmas, positioned by hand on the AD-ES), each as wide as the machine's finger (85 mm on the AD-ES) along the bend line
        const f = shapeMesh(lerpPts(a.finger, b.finger, u), mats.finger, sim.finger_width || 25);
        f.position.z = z;
        add(f);
    }
    const line = lerpPts(a.part, b.part, u);
    for (let i = 0; i < line.length - 1; i++) {
        const [x0, y0] = line[i], [x1, y1] = line[i + 1], L = Math.hypot(x1 - x0, y1 - y0);
        if (L < 1e-6) continue;
        const m = new THREE.Mesh(new THREE.BoxGeometry(L + (i > 0 ? T * 0.15 : 0) + (i < line.length - 2 ? T * 0.15 : 0), T, W), group.userData.hit ? mats.hit : mats.part);
        m.position.set((x0 + x1) / 2, (y0 + y1) / 2, 0);   // free ends stay exactly on the centre-line end (the finger touches that wall)
        m.rotation.z = Math.atan2(y1 - y0, x1 - x0);
        add(m);
    }
}

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
