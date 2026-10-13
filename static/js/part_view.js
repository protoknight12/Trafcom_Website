// The part of a bend program in 3D, folded and unfolded: the flat pattern's contours (holes, notches, rounded corners) are folded along their bend lines.
// data = { L: flat length along the chain, thickness, loops: [[ [s, z], ... ]] (the outer contour first), bends: [{ pos, angle, dir: 'up'|'down', radius, z0, z1 }] }
// s runs along the chain (the neutral fibre: a bend zone is as wide as its bend allowance, K = 0.44, so s IS the arc length), z across it.
// ponytail: all bends parallel (the program model); a hole that straddles a bend zone is left out; polygons are clipped slice by slice (Sutherland-Hodgman), so a
// strongly concave contour can get zero-area triangles; flat shaded facets of ~4 degrees on the bends.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const K = 0.44;

function zonesOf(data, f) {
    const T = data.thickness;
    return data.bends.map(b => {
        const a = b.angle * Math.PI / 180, ba = a * ((b.radius || T) + K * T);
        return { a0: b.pos - ba / 2, a1: b.pos + ba / 2, ba, ang: a * f * (b.dir === 'up' ? 1 : -1), deg: b.angle, b };
    }).sort((p, q) => p.a0 - q.a0);
}

// pose of the neutral line at arc length s when every bend is folded to the fraction f of its angle
function poseFn(zones) {
    return s => {
        let x = 0, y = 0, th = 0, pos = 0;
        for (const z of zones) {
            if (s <= z.a0) break;
            x += Math.cos(th) * (z.a0 - pos); y += Math.sin(th) * (z.a0 - pos); pos = z.a0;
            const sub = Math.min(s, z.a1) - z.a0;
            if (Math.abs(z.ang) < 1e-6) { x += Math.cos(th) * sub; y += Math.sin(th) * sub; }
            else {
                const phi = z.ang * sub / z.ba, inv = Math.sign(z.ang) * z.ba / Math.abs(z.ang);   // 1 / curvature, signed
                x += (Math.sin(th + phi) - Math.sin(th)) * inv; y += (Math.cos(th) - Math.cos(th + phi)) * inv; th += phi;
            }
            pos = Math.min(s, z.a1);
            if (s <= z.a1) return { x, y, th };
        }
        return { x: x + Math.cos(th) * (s - pos), y: y + Math.sin(th) * (s - pos), th };
    };
}

function clip(poly, a, keepGE) {
    const inside = p => keepGE ? p[0] >= a - 1e-9 : p[0] <= a + 1e-9, out = [];
    for (let i = 0; i < poly.length; i++) {
        const p = poly[i], q = poly[(i + 1) % poly.length], ip = inside(p), iq = inside(q);
        if (ip) out.push(p);
        if (ip !== iq) { const t = (a - p[0]) / (q[0] - p[0]); out.push([a, p[1] + (q[1] - p[1]) * t]); }
    }
    return out;
}

// the flat pattern cut into slices along s (at `cuts`) and triangulated ONCE: top faces as [u, z, u, z, u, z] triangles, contour walls as [u0, z0, u1, z1] edges
export function flatSlices(loops, cuts) {
    const [outer, ...holes] = loops, top = [], walls = [];
    const area = p => p.reduce((s, q, i) => s + p[(i + p.length - 1) % p.length][0] * q[1] - q[0] * p[(i + p.length - 1) % p.length][1], 0) / 2;
    const ccw = p => (area(p) < 0 ? p.slice().reverse() : p);   // outer loops counter-clockwise in (u, z), holes clockwise: the walls then all face out of the material
    const first = cuts[0], last = cuts[cuts.length - 1];
    for (let k = 0; k + 1 < cuts.length; k++) {
        const a = cuts[k], b = cuts[k + 1];
        if (b - a < 1e-6) continue;
        const poly = ccw(clip(clip(outer, a, true), b, false));
        if (poly.length < 3) continue;
        const hs = holes.filter(h => Math.min(...h.map(p => p[0])) > a + 1e-6 && Math.max(...h.map(p => p[0])) < b - 1e-6).map(h => ccw(h).reverse());
        const verts = poly.concat(...hs);
        const faces = THREE.ShapeUtils.triangulateShape(poly.map(p => new THREE.Vector2(p[0], p[1])), hs.map(h => h.map(p => new THREE.Vector2(p[0], p[1]))));
        for (const [i, j, l] of faces) {
            let t = [verts[i], verts[j], verts[l]];
            if ((t[1][0] - t[0][0]) * (t[2][1] - t[0][1]) - (t[2][0] - t[0][0]) * (t[1][1] - t[0][1]) < 0) t = [t[0], t[2], t[1]];   // counter-clockwise
            top.push([...t[0], ...t[1], ...t[2]]);
        }
        for (const loop of [poly, ...hs]) {   // the walls along the contour - not along the clip lines inside the part (the ends of the part are real edges)
            for (let i = 0; i < loop.length; i++) {
                const p = loop[i], q = loop[(i + 1) % loop.length];
                const onA = Math.abs(p[0] - a) < 1e-6 && Math.abs(q[0] - a) < 1e-6 && a > first + 1e-6, onB = Math.abs(p[0] - b) < 1e-6 && Math.abs(q[0] - b) < 1e-6 && b < last - 1e-6;
                if (onA || onB) continue;
                walls.push([p[0], p[1], q[0], q[1]]);
            }
        }
    }
    return { top, walls };
}

// the slices placed in space: map(u, z, w) -> [x, y, z] for a point at arc length u, across z, w off the neutral line (+-T/2 = the two faces)
export function mapSlices(sl, T, map) {
    const pos = [], nor = [];
    const face = (p, sign) => { pos.push(p[0], p[1], p[2]); nor.push(sign * p[3], sign * p[4], 0); };
    for (const [u0, z0, u1, z1, u2, z2] of sl.top) {   // counter-clockwise in (u, z): the upper face looks along +w when the order is reversed
        const A = map(u0, z0, T / 2), B = map(u1, z1, T / 2), C = map(u2, z2, T / 2);
        face(A, 1); face(C, 1); face(B, 1);
        const D = map(u0, z0, -T / 2), E = map(u1, z1, -T / 2), F = map(u2, z2, -T / 2);
        face(D, -1); face(E, -1); face(F, -1);
    }
    const tri = (a, b, c) => {
        const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2], vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
        let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
        const l = Math.hypot(nx, ny, nz) || 1;
        for (const p of [a, b, c]) { pos.push(p[0], p[1], p[2]); nor.push(nx / l, ny / l, nz / l); }
    };
    for (const [u0, z0, u1, z1] of sl.walls) {
        const A = map(u0, z0, T / 2), B = map(u1, z1, T / 2), C = map(u1, z1, -T / 2), D = map(u0, z0, -T / 2);
        tri(A, B, C); tri(A, C, D);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3));
    return g;
}

// the cut positions along s: the ends and every bend zone in slices of ~4 degrees; `zoneOf(bend)` -> [a0, a1] in the s coordinate of the loops
export function sliceCuts(L, bends, T, zoneOf) {
    const set = new Set([0, L]);
    for (const b of bends) {
        const [a0, a1] = zoneOf(b), n = Math.max(1, Math.ceil(b.angle / 4));
        for (let i = 0; i <= n; i++) set.add(a0 + (a1 - a0) * i / n);
    }
    return [...set].filter(s => s >= 0 && s <= L).sort((p, q) => p - q);
}

export const bendAllowance = (b, T) => b.angle * Math.PI / 180 * ((b.radius || T) + K * T);

function build(data, f, slices) {
    const T = data.thickness, zones = zonesOf(data, f), pose = poseFn(zones);
    const P = (s, z, w) => { const o = pose(s); return [o.x - Math.sin(o.th) * w, o.y + Math.cos(o.th) * w, z, -Math.sin(o.th), Math.cos(o.th)]; };
    const lines = [];   // the bend lines on the upper face
    for (const z of zones) lines.push([P(z.b.pos, z.b.z0, T / 2 + 0.05), P(z.b.pos, z.b.z1, T / 2 + 0.05), z.b.dir]);
    return { geometry: mapSlices(slices, T, P), lines };
}

// mount(el, data, { interactive, fraction }) - interactive: orbit + a slider unfolded <-> folded; otherwise one still picture (an <img>, so it prints)
export function mountPartView(el, data, opts = {}) {
    if (!data || !data.loops || !data.loops.length) { el.textContent = 'Няма данни за детайла.'; return; }
    const interactive = !!opts.interactive;
    const W = opts.width || el.clientWidth || 560, H = opts.height || 340;
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: !interactive });
    renderer.setPixelRatio(devicePixelRatio);
    renderer.setSize(W, H);
    renderer.setClearColor(interactive ? 0x14171a : 0xffffff);
    const scene = new THREE.Scene();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x556677, 1.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.3);
    scene.add(sun);
    const camera = new THREE.PerspectiveCamera(35, W / H, 0.1, 100000);
    const group = new THREE.Group();
    scene.add(group);
    const mat = new THREE.MeshStandardMaterial({ color: 0xc9d4de, metalness: 0.3, roughness: 0.55, side: THREE.DoubleSide });
    const lineMat = { up: new THREE.LineBasicMaterial({ color: 0x1e9e55 }), down: new THREE.LineBasicMaterial({ color: 0x1f6fd1 }) };
    let fitted = false;
    const slices = flatSlices(data.loops, sliceCuts(data.L, data.bends, data.thickness, b => { const h = bendAllowance(b, data.thickness) / 2; return [b.pos - h, b.pos + h]; }));

    const show = f => {
        group.children.slice().forEach(o => { group.remove(o); o.geometry && o.geometry.dispose(); });
        const { geometry, lines } = build(data, f, slices);
        group.add(new THREE.Mesh(geometry, mat));
        for (const [a, b, dir] of lines) group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(...a), new THREE.Vector3(...b)]), lineMat[dir] || lineMat.up));
        if (!fitted) {   // the camera looks at the folded part and stays there while the slider moves
            const box = new THREE.Box3().setFromObject(group);
            const c = box.getCenter(new THREE.Vector3()), r = Math.max(box.getSize(new THREE.Vector3()).length(), 1);
            camera.position.set(c.x + r * 0.9, c.y + r * 0.8, c.z + r * 1.3);
            camera.up.set(0, 1, 0);
            camera.lookAt(c);
            sun.position.copy(camera.position);
            if (controls) { controls.target.copy(c); controls.update(); }
            fitted = true;
        }
    };
    let controls = null;
    if (interactive) {
        controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
    }
    // fit to the folded shape first, then show the requested state
    const f0 = opts.fraction == null ? 1 : opts.fraction;
    show(1);
    if (f0 !== 1) show(f0);
    if (!interactive) {
        renderer.render(scene, camera);
        const img = new Image();
        img.src = renderer.domElement.toDataURL('image/png');
        img.style.cssText = 'max-width:100%;border-radius:6px';
        el.replaceChildren(img);
        renderer.dispose();
        return;
    }
    el.replaceChildren(renderer.domElement);
    const slider = opts.slider;
    if (slider) slider.addEventListener('input', () => show(slider.value / 100));
    renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
}
