// 3D view of a press brake and of a bend program: the cross-sections from bending.profile.step_frames (mm, x depth / y up) extruded along z (the bend line),
// inside a schematic machine built from the machine's real dimensions (PressBrakeConfig + the controller's parameters). Every element can be shown/hidden.
// ponytail: frames are linearly interpolated (no physics); frame, ram, bed ... are boxes at the real overall sizes (the catalogue gives no outline);
// tools are as wide as the part + margin; the part is boxes per centre-line segment. The fingers use the controller's real plan outlines.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { flatSlices, mapSlices, sliceCuts, bendAllowance } from './part_view.js';

const sim = JSON.parse(document.getElementById('simdata').textContent);
const M = sim.machine || {};
const view = document.getElementById('view');
const $ = id => document.getElementById(id);
if (!sim.steps.length) { view.textContent = 'Няма стъпки с инструменти за симулация.'; throw new Error('no steps'); }

const ELEMENTS = [   // key, label, group, shown by default
    ['part', 'Детайл', 'Процес', 1], ['punch', 'Поансон + закрепване', 'Процес', 1], ['die', 'Матрица', 'Процес', 1], ['fingers', 'Упори (език)', 'Процес', 1],
    ['ram', 'Горна греда (Y1/Y2)', 'Машина', 1], ['clamps', 'Скоби на горния инструмент', 'Машина', 1], ['dieholder', 'Държач на матрицата', 'Машина', 1],
    ['clamp_parts', 'Скоби: планки, болтове, лостове', 'Машина', 1], ['holder_plates', 'Държач: свързващи планки', 'Машина', 1], ['crowning', 'Бомбиране', 'Машина', 1], ['bed', 'Долна маса', 'Машина', 1], ['frames', 'Странични рамки', 'Машина', 1],
    ['bg_finger', 'Заден упор: упори (тяло, водачи)', 'Заден упор', 1], ['bg_beam', 'Заден упор: греда и колички', 'Заден упор', 1], ['bg_columns', 'Заден упор: колони R', 'Заден упор', 1], ['bg_rails', 'Заден упор: водачи X', 'Заден упор', 1],
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
    arm: mat(0x7d8a97), fbody: mat(0x3d7a52), panel: mat(0x22262b), screen: mat(0x2f8fbf), floor: mat(0x1d2126) };

mats.part.side = mats.hit.side = THREE.DoubleSide;
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
    const ST = sim.steps[0].static;   // the bed / crowning / die-holder polygons of bending.profile (also what the collision check uses)
    for (const [key, poly, m] of [['bed', ST.bed, mats.bed], ['crowning', ST.crowning, mats.crowning]]) staticG.add(tag(shapeMesh(poly, m, WL), key));
    // die holder: the sections of the real holder (e.g. 550 + 1050 + 450)
    // the holder is a U channel: the 60 mm base of the die sits in it, the walls hug the die's lower 20 mm; sections are joined by plates on both faces
    const U = ST.die_holder;
    let z0 = -WL / 2;
    for (const len of (M.die_holder || [WL])) {
        const h = tag(shapeMesh(U, mats.holder, len - 2), 'dieholder');
        h.position.z = z0 + len / 2; staticG.add(h);
        if (z0 > -WL / 2 + 1) for (const sx of [-92, 92]) box('holder_plates', mats.rail, 4, 60, 160, sx, -75, z0);   // plates on the faces of the U
        z0 += len;
    }
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
let cur = 0, time = 0, playing = false, dynamic = [], stepStatic = [], stopAt = -1, stopped = false, focusAnim = null;
// the tool as the pieces picked for this bend (bending.tools.pick_segments), left to right, centred on the part; without a set one piece as long as the part
function segmentsOf(st) {
    const L = st.layout;
    if (!L) return [[0, TOOL_W]];
    let z = -L.total / 2;
    return L.segments.map(l => { const r = [z + l / 2, l - 0.8]; z += l; return r; });
}
function loadStep(i) {
    cur = i; time = 0; $('step').value = i;
    stepStatic.forEach(m => { staticG.remove(m); m.geometry.dispose(); });
    const st = sim.steps[i];
    stepStatic = [];
    for (const [z, len] of segmentsOf(st)) { const d = tag(shapeMesh(st.die, mats.die, len), 'die'); d.position.z = z; staticG.add(d); stepStatic.push(d); }
    const hit = st.hits.length > 0;
    $('warn').style.display = hit || (M.warn || []).length ? 'block' : 'none';
    $('warn').textContent = [hit ? 'КОЛИЗИЯ: ' + st.hits.join('; ') : '', ...(M.warn || [])].filter(Boolean).join(' · ');
    $('label').textContent = `Стъпка ${st.seq}: ${st.punch} / ${st.die_code} V${st.v} · ${st.angle}°` + (M.finger_machine_z ? ` · Z1 ${M.finger_machine_z[0]} / Z2 ${M.finger_machine_z[1]}` : '');
    dynG.userData.hit = hit;
    // the first collision: playback stops on the frame where it happens (see bending.profile.step_trajectory), the camera goes there
    const fh = st.first_hit;
    stopAt = fh ? Math.max(0, st.frames.findIndex(fr => fr.angle >= fh.angle - 0.01)) : -1;
    if (fh && stopAt < 0) stopAt = st.frames.length - 1;
    stopped = false; hitMark.visible = false;
}
const hitMark = new THREE.Mesh(new THREE.SphereGeometry(5, 16, 12), new THREE.MeshBasicMaterial({ color: 0xff3b30, transparent: true, opacity: 0.85, depthTest: false }));
hitMark.renderOrder = 10; hitMark.visible = false; scene.add(hitMark);
// fly the camera to the collision (a plate hit has no cross-section place: the side plate, half-way between the frames)
function focusHit(fh) {
    const to = new THREE.Vector3(...(fh.at ? [fh.at[0], fh.at[1], 0] : [0, 150, BF / 2]));
    hitMark.position.copy(to); hitMark.visible = true;
    // a cross-section hit: look along the bend line from far away through a narrow lens, so the profile at the collision fills the screen
    const dir = fh.at ? new THREE.Vector3(-0.12, 0.08, 1).normalize() : camera.position.clone().sub(controls.target).normalize();
    focusAnim = { t0: performance.now(), t: controls.target.clone(), p: camera.position.clone(), fov: camera.fov, to, pos: to.clone().addScaledVector(dir, fh.at ? 1500 : 900), toFov: fh.at ? 9 : 40 };
}

// The outline as flat rectangles: for every band of depth between two vertices the lateral extent of the polygon (cut-outs and roundings filled), neighbouring bands
// that differ by less than 3 mm merged; the nose-to-full-width part is one plate. ponytail: the controller's outline has hooks and curves the real tongue does not need to show.
function flatRects(outline) {
    const ys = [...new Set(outline.map(p => Math.round(p[1] * 100) / 100))].sort((a, b) => a - b), bands = [];
    for (let i = 0; i < ys.length - 1; i++) {
        const y = (ys[i] + ys[i + 1]) / 2, xs = [];
        outline.forEach((p, k) => {
            const q = outline[(k + 1) % outline.length];
            if ((p[1] > y) !== (q[1] > y)) xs.push(p[0] + (y - p[1]) * (q[0] - p[0]) / (q[1] - p[1]));
        });
        if (xs.length) bands.push({ y0: ys[i], y1: ys[i + 1], x0: Math.min(...xs), x1: Math.max(...xs) });
    }
    // the tongue: everything from the nose to the end of the full-width part is one flat plate (the hook/notch at the nose is dropped)
    const maxW = Math.max(...bands.map(b => b.x1 - b.x0)), wide = bands.filter(b => b.x1 - b.x0 >= 0.95 * maxW);
    if (wide.length) {
        const w = { y0: bands[0].y0, y1: wide[wide.length - 1].y1, x0: Math.min(...wide.map(b => b.x0)), x1: Math.max(...wide.map(b => b.x1)) };
        bands.splice(0, bands.findIndex(b => b.y0 >= w.y1 - 1e-6) < 0 ? bands.length : bands.findIndex(b => b.y0 >= w.y1 - 1e-6), w);
    }
    const out = [];
    for (const b of bands) {
        const l = out[out.length - 1];
        if (l && Math.abs(l.x0 - b.x0) < 3 && Math.abs(l.x1 - b.x1) < 3) { l.y1 = b.y1; l.x0 = Math.min(l.x0, b.x0); l.x1 = Math.max(l.x1, b.x1); }
        else out.push({ ...b });
    }
    return out.filter(r => r.y1 - r.y0 > 0.5 && r.x1 - r.x0 > 0.5);
}

function fingerMeshes(poly, add, beamX) {
    const nose = Math.min(...poly.map(p => p[0])), base = Math.min(...poly.map(p => p[1]));
    const FA = M.finger_asm;
    if (FA) {   // the body behind the tongue (drawing pages 32-33); the tongue below is the controller's
        (M.finger_z || [-60, 60]).forEach(zp => FA.parts.forEach(({ key, box: [u0, u1, h0, h1, w0, w1] }) => {
            u1 = Math.min(u1, beamX - 5 - nose);   // the finger is mounted on the beam's front: nothing goes through it
            if (u1 - u0 < 1) return;
            const m = tag(new THREE.Mesh(new THREE.BoxGeometry(u1 - u0, h1 - h0, w1 - w0), mats.fbody), key);
            m.position.set(nose + (u0 + u1) / 2, base + (h0 + h1) / 2, zp + (w0 + w1) / 2);
            add(m);
        }));
    }
    (M.finger_z || [-60, 60]).forEach((zp, n) => {
        const data = M.finger && (n ? M.finger.z2 : M.finger.z1);
        if (!data) {   // no outline data: the cross-section as a block
            const g = shapeMesh(poly, mats.finger, 85); g.position.z = zp; add(tag(g, 'fingers')); return;
        }
        for (const lv of data.levels) {
            // plan outline (x = lateral, y = depth from the nose) -> flat rectangles (no roundings, no cut-outs): scene x = depth, z = lateral; each level is extruded up by its height
            for (const r of flatRects(lv.outline)) {
                const m = tag(new THREE.Mesh(new THREE.BoxGeometry(r.y1 - r.y0, lv.height, r.x1 - r.x0), mats.finger), 'fingers');
                m.position.set(nose + lv.offset[1] + (r.y0 + r.y1) / 2, base + lv.offset[2] + lv.height / 2, zp + (r.x0 + r.x1) / 2);
                add(m);
            }
        }
    });
}

function draw(f) {
    dynamic.forEach(m => { dynG.remove(m); m.geometry.dispose(); });
    dynamic = [];
    const st = sim.steps[cur], fr = st.frames;
    const k = Math.min(Math.floor(f), fr.length - 1), a = fr[k], b = fr[Math.min(k + 1, fr.length - 1)], u = f - k;
    const add = m => { if (m.parent !== dynG) dynG.add(m); dynamic.push(m); return m; };
    const punch = lerpPts(a.punch, b.punch, u), clamp = lerpPts(a.clamp, b.clamp, u);
    for (const [z, len] of segmentsOf(st)) {
        for (const [poly, m] of [[punch, mats.punch], [clamp, mats.clamp]]) { const t = tag(shapeMesh(poly, m, len), 'punch'); t.position.z = z; add(t); }
    }
    // the upper beam and its tool clamps hang on the top of the punch's tang
    // every body of the machine is drawn from the polygons the collision check uses (bending.profile.machine_env)
    const env = {};
    for (const k of Object.keys(a.env)) env[k] = lerpPts(a.env[k], b.env[k], u);
    const at = (poly, key, m, depth, z) => { const o = tag(shapeMesh(poly, m, depth), key); o.position.z = z; return add(o); };
    at(env.ram, 'ram', mats.ram, WL, 0);   // the upper table: a tall flat plate whose ends run in the guides of the plates
    const n = M.clamps || 10, cl = WL / n, bl = cl - 20;
    const bx = env.holder_blade.map(p => p[0]), by = env.holder_blade.map(p => p[1]);
    for (let i = 0; i < n; i++) {
        const zc = -WL / 2 + cl * (i + 0.5);
        at(env.holder_body, 'clamps', mats.clamp, bl, zc);   // the clamp body round the 13 mm tang
        at(env.holder_rail, 'clamps', mats.clamp, bl, zc);   // the rail it hangs on
        at(env.holder_blade, 'clamp_parts', mats.rail, bl * 0.65, zc);   // the clamping blade on the operator side
        for (const dz of [-0.22, 0.22]) {   // its two M10 bolts
            const bolt = tag(new THREE.Mesh(new THREE.CylinderGeometry(8, 8, 12, 12), mats.punch), 'clamp_parts');
            bolt.rotation.z = Math.PI / 2; bolt.position.set(Math.min(...bx) - 6, (Math.min(...by) + Math.max(...by)) / 2 + 12, zc + dz * bl); add(bolt);
        }
        const lv = box('clamp_parts', mats.beam, 22, 14, 70, Math.min(...bx) - 10, Math.min(...by) - 6, zc + bl * 0.3, dynG);   // the quick-release lever
        lv.rotation.z = -0.6; add(lv);
    }
    (M.finger_z || [-60, 60]).forEach(zp => at(env.bg_column, 'bg_columns', mats.beam, 70, zp));   // the R columns behind the fingers
    at(env.bg_beam, 'bg_beam', mats.beam, 1450, 0);                                                  // the X carriage beam
    const bnose = Math.min(...env.bg_beam.map(p => p[0]));
    for (const sgn of [-1, 1]) add(box('bg_beam', mats.beam, 130, 90, 90, bnose + 55, Math.min(...env.bg_beam.map(p => p[1])) + 20, sgn * (BF / 2 - 90), dynG));
    fingerMeshes(lerpPts(a.finger, b.finger, u), add, bnose);
    const partMat = dynG.userData.hit && stopAt >= 0 && f >= stopAt ? mats.hit : mats.part;
    const ps = partSlices(cur);
    const strip = ps ? tag(new THREE.Mesh(realPart(ps, lerpPts(a.part, b.part, u), st.inner_radius), partMat), 'part')
                     : tag(shapeMesh(sheetOutline(lerpPts(a.part, b.part, u), T, st.inner_radius), partMat, st.bend_len || W), 'part');   // no flat pattern known: the cross-section as a strip
    add(strip);
}

// The part as it really is: the detail's flat pattern (outline, holes, notches, rounded corners - sim.part, from its DXF) folded onto the centre line of the cross-section.
// The flat pattern is cut into slices and triangulated once per step (partSlices); every frame only places the vertices on the current centre line (realPart).
// Orientation: the gauge end of the flat pattern is the back end of the chain; a part that has to be turned over (a bend down) is mirrored across the bend line.
// ponytail: only the current step's orientation is drawn; the flat length is stretched to the centre-line length (they differ by <1% - K 0.44 vs 0.5).
const partCache = new Map();
function partSlices(i) {
    if (!sim.part) return null;
    if (!partCache.has(i)) {
        const D = sim.part, st = sim.steps[i], L = D.L, flip = st.back_end === 'left', zs = (flip ? -1 : 1) * (st.bend_dir === 'down' ? -1 : 1);
        const uOf = c => (flip ? L - c : c);
        const cuts = sliceCuts(L, D.bends, D.thickness, b => { const h = bendAllowance(b, D.thickness) / 2, c = uOf(b.pos); return [c - h, c + h]; });
        partCache.set(i, flatSlices(D.loops.map(lp => lp.map(([c, w]) => [uOf(c), zs * w])), cuts));
    }
    return partCache.get(i);
}
function realPart(slices, line, rIn) {
    const path = sheetPath(line, T, rIn), cum = [0];
    for (let i = 1; i < path.length; i++) cum.push(cum[i - 1] + Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]));
    const k = cum[cum.length - 1] / sim.part.L;
    const ang = path.map((q, i) => { const a = path[Math.max(i - 1, 0)], b = path[Math.min(i + 1, path.length - 1)]; return Math.atan2(b[1] - a[1], b[0] - a[0]); });   // the tangent angle at each vertex: smooth shading round the bends
    return mapSlices(slices, T, (u, z, w) => {
        const s = Math.min(Math.max(u * k, 0), cum[cum.length - 1]);
        let lo = 0, hi = cum.length - 2;
        while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (cum[mid] <= s) lo = mid; else hi = mid - 1; }
        const seg = cum[lo + 1] - cum[lo] || 1, t = (s - cum[lo]) / seg;
        const dx = (path[lo + 1][0] - path[lo][0]) / seg, dy = (path[lo + 1][1] - path[lo][1]) / seg;
        const th = ang[lo] + (ang[lo + 1] - ang[lo]) * t, nx = -Math.sin(th), ny = Math.cos(th);
        return [path[lo][0] + dx * seg * t - dy * w, path[lo][1] + dy * seg * t + dx * w, z, nx, ny];
    });
}

// The sheet as ONE solid strip: the centre line with every bend rounded with the inner radius the calculation uses (bending.profile: 0.16 V), offset by +-T/2.
// The outline is (left side) + (right side reversed). Free ends stay exactly on the centre-line ends (the finger touches that wall).
// ponytail: one radius rule for every material (0.16 V; aluminium is a bit tighter, stainless a bit wider); a leg shorter than the arc needs gets a smaller radius.
function sheetPath(line, T, rIn) {
    const p = line.filter((q, i) => !i || Math.hypot(q[0] - line[i - 1][0], q[1] - line[i - 1][1]) > 1e-6);
    const dir = (a, b) => { const l = Math.hypot(b[0] - a[0], b[1] - a[1]); return [(b[0] - a[0]) / l, (b[1] - a[1]) / l]; };
    const path = [p[0]];
    for (let i = 1; i < p.length - 1; i++) {
        const d0 = dir(p[i - 1], p[i]), d1 = dir(p[i], p[i + 1]);
        const cross = d0[0] * d1[1] - d0[1] * d1[0], phi = Math.atan2(Math.abs(cross), d0[0] * d1[0] + d0[1] * d1[1]);
        if (phi < 1e-3) { path.push(p[i]); continue; }
        const l0 = Math.hypot(p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]), l1 = Math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
        const tl = Math.min((rIn + T / 2) * Math.tan(phi / 2), 0.45 * l0, 0.45 * l1), R = tl / Math.tan(phi / 2), s = Math.sign(cross);
        const A = [p[i][0] - d0[0] * tl, p[i][1] - d0[1] * tl], C = [A[0] - s * d0[1] * R, A[1] + s * d0[0] * R];   // the centre is on the inside of the turn
        const a0 = Math.atan2(A[1] - C[1], A[0] - C[0]), N = Math.max(4, Math.ceil(phi / 0.15));
        for (let k = 0; k <= N; k++) { const a = a0 + s * phi * k / N; path.push([C[0] + R * Math.cos(a), C[1] + R * Math.sin(a)]); }
    }
    path.push(p[p.length - 1]);
    return path;
}
function sheetOutline(line, T, rIn) {
    const dir = (a, b) => { const l = Math.hypot(b[0] - a[0], b[1] - a[1]); return [(b[0] - a[0]) / l, (b[1] - a[1]) / l]; };
    const path = sheetPath(line, T, rIn);
    const nrm = i => { const a = path[Math.max(i - 1, 0)], b = path[Math.min(i + 1, path.length - 1)], d = dir(a, b); return [-d[1], d[0]]; };
    const side = sg => path.map((q, i) => { const n = nrm(i); return [q[0] + sg * n[0] * T / 2, q[1] + sg * n[1] * T / 2]; });
    return side(1).concat(side(-1).reverse());
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
const cam = (pos, tgt) => { camera.fov = 40; camera.updateProjectionMatrix(); camera.position.set(...pos); controls.target.set(...tgt); controls.update(); };
const VIEWS = { tool: () => cam([-160, 120, 380], [0, 20, 0]), all: () => cam([-2600, 1200, 3200], [150, 300, 0]),
    front: () => cam([-3800, 250, 0], [150, 300, 0]), side: () => cam([150, 300, 4200], [150, 300, 0]), top: () => cam([150, 4600, 1], [150, 0, 0]) };
Object.keys(VIEWS).forEach(k => { $('v_' + k).onclick = VIEWS[k]; });
VIEWS.tool();

sim.steps.forEach((s, i) => $('step').add(new Option(`Стъпка ${s.seq} · ${s.angle}°`, i)));
// starts paused; "Старт" runs ONE step, then the next step is loaded and waits paused at its start (the last one wraps to the first)
const setPlay = on => { playing = on; $('play').innerHTML = on ? '&#9208; Пауза' : '&#9654; Старт'; };
const goStep = i => { loadStep(i); setPlay(false); focusAnim = null; if (camera.fov !== 40) { camera.fov = 40; camera.updateProjectionMatrix(); } };
$('step').onchange = e => goStep(+e.target.value);
$('prev').onclick = () => goStep((cur + sim.steps.length - 1) % sim.steps.length);
$('next').onclick = () => goStep((cur + 1) % sim.steps.length);
$('play').onclick = () => {
    setPlay(!playing);
};

const FPS = 4;   // frames of the stroke per second at 1x
let last = performance.now();
function resize() { const w = view.clientWidth, h = view.clientHeight; renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); }
addEventListener('resize', resize); resize();
loadStep(0); setPlay(false);
(function loop(now) {
    const dt = (now - last) / 1000; last = now;
    const n = sim.steps[cur].frames.length - 1;
    if (playing) {
        time = Math.min(n, time + dt * FPS * +$('speed').value);
        if (stopAt >= 0 && !stopped && time >= stopAt) { time = stopAt; stopped = true; setPlay(false); focusHit(sim.steps[cur].first_hit); }   // stop AT the collision
        else if (time >= n) goStep((cur + 1) % sim.steps.length);   // the step is done: the next one waits paused at its start
    }
    draw(Math.min(time, n));
    if (focusAnim) {   // ease the camera and its target to the collision
        const k = Math.min(1, (performance.now() - focusAnim.t0) / 700), e = k * k * (3 - 2 * k);
        controls.target.lerpVectors(focusAnim.t, focusAnim.to, e); camera.position.lerpVectors(focusAnim.p, focusAnim.pos, e);
        camera.fov = focusAnim.fov + (focusAnim.toFov - focusAnim.fov) * e; camera.updateProjectionMatrix();
        if (k >= 1) focusAnim = null;
    }
    if (hitMark.visible) hitMark.scale.setScalar(1 + 0.35 * Math.sin(now / 150));
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(loop);
})(last);
