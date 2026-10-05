// Procedural models + textures for the /factory3d hall scene. Every builder fills a THREE.Group whose
// origin is the centre of the footprint on the floor, front = +Z; w = X extent, d = Z extent, h = height (m).
import * as THREE from 'three';

const std = (color, o = {}) => new THREE.MeshStandardMaterial(Object.assign({ color, roughness: 0.6, metalness: 0.15 }, o));
export const MAT = {
    white: std(0xeceff2), beige: std(0xe2d9c3), steel: std(0xa3acb6, { metalness: 0.5, roughness: 0.4 }), grey: std(0x8a929b),
    dark: std(0x23272e), black: std(0x111317), navy: std(0x12203b), red: std(0xc41e2c), yellow: std(0xf2b600), blue: std(0x1d4ed8),
    teal: std(0x14b8a6), orange: std(0xf26b1d), wood: std(0xb08968), burgundy: std(0x6b1d2a),
    glass: std(0x101a28, { roughness: 0.1, metalness: 0.7, transparent: true, opacity: 0.9 }),
    screen: std(0x08121f, { emissive: 0x0a3a66, emissiveIntensity: 0.9 }),
    green: std(0x22e06b, { emissive: 0x22e06b, emissiveIntensity: 0.7 }),
};

function box(g, w, h, d, x, y, z, mat) {                       // centre x/z, bottom y
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.position.set(x, y + h / 2, z);
    g.add(m);
    return m;
}
function cyl(g, r, len, x, y, z, mat, axis = 'y') {            // axis 'z' / 'x' = lying along Z / X
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 18), mat);
    if (axis === 'z') m.rotation.x = Math.PI / 2;
    if (axis === 'x') m.rotation.z = Math.PI / 2;
    m.position.set(x, y, z);
    g.add(m);
    return m;
}
function pult(g, w, h, x, y, z) {                              // control panel: dark housing + screen on its front
    box(g, w, h, 0.12, x, y, z, MAT.dark);
    box(g, w * 0.7, h * 0.45, 0.02, x, y + h * 0.4, z + 0.065, MAT.screen);
}

// ---------- machines ----------
const MACHINE = {
    // vertical machining centre: white cabin, big dark sliding-door window, pult on the right, chip conveyor at the back
    vmc(g, w, d, h) {
        box(g, w, 0.15, d, 0, 0, 0, MAT.steel);
        box(g, w * 0.96, h - 0.15, d * 0.8, 0, 0.15, -d * 0.08, MAT.white);
        const fz = d * 0.32;
        box(g, w * 0.55, (h - 0.15) * 0.55, 0.03, -w * 0.1, h * 0.28, fz + 0.02, MAT.glass);
        box(g, 0.04, h * 0.3, 0.05, w * 0.22, h * 0.4, fz + 0.05, MAT.dark);
        pult(g, Math.min(0.5, w * 0.28), 0.5, w / 2 - Math.min(0.5, w * 0.28) / 2 - 0.02, h * 0.45, d / 2 - 0.1);
        box(g, w * 0.5, h * 0.22, d * 0.2, 0, 0.15, -d * 0.45, MAT.steel);
        box(g, 0.25, 0.3, 0.25, -w * 0.3, h, -d * 0.1, MAT.dark);
    },
    // DMG DMU 75 monoblock: white cabin with the big window + the tall dark electrical cabinet and chip conveyor/bin beside it
    dmu(g, w, d, h) {
        const mw = w * 0.62, cx = -w / 2 + mw / 2;
        const main = new THREE.Group();
        main.position.x = cx;
        MACHINE.vmc(main, mw, d, h);
        g.add(main);
        box(g, w * 0.34, Math.min(h, 2.8), d * 0.6, w / 2 - w * 0.17, 0, -d * 0.18, MAT.dark);
        box(g, w * 0.3, 0.08, d * 0.25, w / 2 - w * 0.17, 0.5, d * 0.3, MAT.navy);
        box(g, w * 0.3, 0.35, d * 0.28, w / 2 - w * 0.17, 0, d * 0.3, MAT.steel);
    },
    // CNC lathe / turn-mill: beige body, front window, pult
    lathe(g, w, d, h) {
        box(g, w, 0.18, d, 0, 0, 0, MAT.steel);
        box(g, w * 0.92, h * 0.72, d * 0.8, 0, 0.18, -d * 0.06, MAT.beige);
        box(g, w * 0.4, h * 0.14, d * 0.5, -w * 0.2, 0.18 + h * 0.72, -d * 0.06, MAT.beige);
        box(g, w * 0.55, h * 0.38, 0.03, -w * 0.1, h * 0.34, d * 0.34 + 0.02, MAT.glass);
        pult(g, Math.min(0.45, w * 0.3), 0.4, w / 2 - Math.min(0.45, w * 0.3) / 2 - 0.02, h * 0.5, d / 2 - 0.08);
    },
    // a lathe whose bar feeder is a separately sized accessory (see buildAccessory)
    bar_lathe(g, w, d, h) { MACHINE.lathe(g, w, d, h); },
    // sheet-metal laser: low table, gantry beam across it, white cabin with window at the end
    laser(g, w, d, h) {
        const bw = w * 0.78, bx = -w * 0.11;
        box(g, bw, 0.85, d * 0.92, bx, 0, 0, MAT.grey);
        [-1, 1].forEach(s => box(g, bw, 0.12, 0.12, bx, 0.85, s * d * 0.46, MAT.dark));
        const gx = bx - bw * 0.1;
        [-1, 1].forEach(s => box(g, 0.5, 0.5, 0.3, gx, 0.85, s * d * 0.46, MAT.dark));
        box(g, 0.5, 0.35, d * 0.98, gx, 1.35, 0, MAT.dark);
        box(g, 0.3, 0.3, 0.3, gx, 1.0, 0, MAT.steel);
        box(g, w * 0.2, h, d * 0.8, w * 0.4, 0, 0, MAT.white);
        box(g, w * 0.14, h * 0.4, 0.03, w * 0.4, h * 0.45, d * 0.4 + 0.02, MAT.glass);
        box(g, 0.12, 0.3, 0.12, w * 0.4, h, 0, MAT.yellow);
    },
    // enclosed (cabin) laser: full white safety cabin with a big dark window, yellow warning strip, console in front, chiller/extraction on top
    laser_cabin(g, w, d, h) {
        box(g, w, 0.12, d, 0, 0, 0, MAT.steel);
        box(g, w, h - 0.12, d * 0.86, 0, 0.12, -d * 0.07, MAT.white);
        const fz = d * 0.36;
        box(g, w * 0.7, (h - 0.12) * 0.5, 0.03, -w * 0.05, h * 0.3, fz + 0.02, MAT.glass);
        [-1, 1].forEach(s => box(g, 0.05, (h - 0.12) * 0.5, 0.05, -w * 0.05 + s * w * 0.35, h * 0.3, fz + 0.04, MAT.grey));
        box(g, w * 0.7, 0.12, 0.04, -w * 0.05, 0.3, fz + 0.03, MAT.yellow);
        box(g, 0.05, h * 0.25, 0.06, w * 0.3, h * 0.4, fz + 0.06, MAT.dark);
        pult(g, 0.5, 0.9, w / 2 - 0.35, 0.9, d / 2 - 0.08);
        box(g, w * 0.3, 0.4, d * 0.4, -w * 0.3, h, -d * 0.1, MAT.dark);
        box(g, 0.12, 0.3, 0.12, w * 0.3, h, -d * 0.1, MAT.yellow);
    },
    // industrial robot manipulator: base plate, pedestal, orange turret + lower/upper arm reaching to the front (+Z), wrist with gripper
    robot(g, w, d, h, color = MAT.orange) {
        const r = Math.min(w, d) / 2, py = 0.08;
        box(g, w, py, d, 0, 0, 0, MAT.steel);
        cyl(g, r * 0.7, h * 0.18, 0, py + h * 0.09, 0, MAT.dark);
        cyl(g, r * 0.6, h * 0.1, 0, py + h * 0.23, 0, color);
        const lower = new THREE.Group();
        lower.position.set(0, py + h * 0.28, 0);
        lower.rotation.x = 0.15;                                   // leans slightly toward the front
        g.add(lower);
        box(lower, r * 0.5, h * 0.42, r * 0.5, 0, 0, 0, color);
        cyl(lower, r * 0.3, r * 0.6, 0, h * 0.42, 0, color, 'x');
        const upper = new THREE.Group();
        upper.position.set(0, h * 0.42, 0);
        lower.add(upper);
        box(upper, r * 0.4, r * 0.4, h * 0.45, 0, -r * 0.2, h * 0.225, color);
        box(upper, r * 0.3, r * 0.3, r * 0.5, 0, -r * 0.15, h * 0.45 + r * 0.25, MAT.dark);                 // wrist
        [-1, 1].forEach(s => box(upper, 0.03, r * 0.35, 0.04, s * r * 0.12, -r * 0.5, h * 0.45 + r * 0.45, MAT.steel));   // gripper fingers
    },
    // Polymeta LS3015 vacuum sheet loader (300 kg), Г-shaped pillar jib: black column on an angled flange, black I-beam arm with a
    // trolley rail, silver vacuum cylinder on the trolley, black frame (3.6 m for the 300 kg model) with ~10 suction cups hanging
    // over a pallet of sheets (max 3000 x 1500). Column at the -X end; footprint ~5.8 x 1.6 m, height ~3.2 m.
    sheet_lift(g, w, d, h) {
        const cx = -w / 2 + 0.3, fL = Math.min(3.6, (w - 0.3) * 0.62), R = (w - 0.3) - fL / 2;   // column x, frame length, arm reach to frame centre
        const fx = cx + R, black = MAT.dark;
        box(g, 0.6, 0.04, 0.6, cx, 0, 0, black);                                               // angled flange plate
        [[-1, 0], [1, 0], [0, -1], [0, 1]].forEach(([sx, sz]) => box(g, sx ? 0.2 : 0.04, 0.35, sz ? 0.2 : 0.04, cx + sx * 0.2, 0.04, sz * 0.2, black));   // gussets
        box(g, 0.3, h - 0.3, 0.3, cx, 0.04, 0, black);                                         // column
        box(g, R + 0.2, 0.28, 0.2, cx + (R + 0.2) / 2 - 0.15, h - 0.3, 0, black);              // arm (I-beam)
        box(g, R - 0.3, 0.06, 0.1, cx + (R - 0.3) / 2 + 0.2, h - 0.02, 0, MAT.grey);           // trolley rail on top
        box(g, 0.35, 0.22, 0.28, fx, h - 0.5, 0, MAT.grey);                                    // trolley
        cyl(g, 0.07, 1.0, fx, h - 1.05, 0, std(0xcfd4da, { metalness: 0.7, roughness: 0.3 })); // silver vacuum / lift cylinder
        const fy = Math.max(0.9, h - 1.9);                                                      // frame hangs here
        box(g, fL, 0.06, 0.08, fx, fy, 0, black);                                              // main bar (3.6 m)
        box(g, 0.28, 0.1, 0.28, fx, fy + 0.06, 0, std(0x1f7a4d));                              // green vacuum block
        [-0.38, -0.13, 0.13, 0.38].forEach(f => {                                              // cross arms with a cup at each end
            box(g, 0.05, 0.05, 1.2, fx + f * fL, fy, 0, black);
            [-1, 1].forEach(s => cyl(g, 0.1, 0.1, fx + f * fL, fy - 0.07, s * 0.6, MAT.black));
        });
        [-1, 1].forEach(s => cyl(g, 0.1, 0.1, fx + s * (fL / 2 - 0.1), fy - 0.07, 0, MAT.black));   // cups at the ends of the main bar
        box(g, 3.0, 0.12, 1.5, fx, 0, 0, MAT.wood);                                             // pallet
        box(g, 2.96, 0.3, 1.46, fx, 0.12, 0, std(0xc9d1d9, { metalness: 0.6, roughness: 0.35 }));   // stack of 3000 x 1500 sheets
    },
    robot_green(g, w, d, h) { MACHINE.robot(g, w, d, h, std(0x5cb82e)); },   // LT1500-C-6 is green in its manual
    // press brake (Durma AD-R 40175 proportions: bending length 4.05 m and 3.6 m between the columns inside a 5.25 m long machine):
    // two uprights, upper beam, lower die, yellow guard, console at the right end
    press(g, w, d, h) {
        const bl = w * 0.77, ux = w * 0.38;
        box(g, bl, 0.3, d * 0.7, 0, 0, -d * 0.1, MAT.steel);
        [-1, 1].forEach(s => box(g, 0.4, h, d * 0.7, s * ux, 0, -d * 0.1, MAT.grey));
        box(g, bl + 0.6, h * 0.22, d * 0.6, 0, h * 0.72, -d * 0.1, MAT.grey);
        box(g, bl * 0.95, 0.2, 0.2, 0, h * 0.4, d * 0.05, MAT.steel);
        box(g, bl * 0.9, h * 0.35, 0.03, 0, h * 0.2, d * 0.3, std(0xf2b600, { transparent: true, opacity: 0.35 }));
        box(g, 0.5, 1.8, 0.8, w / 2 - 0.3, 0, -d * 0.2, MAT.white);                                       // electrical cabinet
        pult(g, 0.4, 0.8, w / 2 - 0.45, 0.5, d / 2 - 0.2);
    },
    compressor(g, w, d, h) {
        box(g, w, h, d, 0, 0, 0, MAT.navy);
        box(g, w * 0.5, h * 0.2, 0.03, 0, h * 0.62, d / 2 + 0.02, MAT.screen);
        box(g, 0.03, h * 0.4, d * 0.6, w / 2 + 0.02, h * 0.15, 0, MAT.dark);
    },
    // big dark cabinet-type machine with a chip bin in front
    dark_box(g, w, d, h) {
        box(g, w, h, d * 0.7, 0, 0, -d * 0.15, MAT.dark);
        box(g, w * 0.8, h * 0.28, d * 0.3, 0, 0.1, d * 0.35, MAT.steel);
        pult(g, 0.35, 0.4, w / 2 - 0.3, h * 0.4, d * 0.2 + 0.1);
    },
    box(g, w, d, h) { box(g, w, h, d, 0, 0, 0, MAT.grey); },
};
export const MACHINE_MODELS = Object.keys(MACHINE);

// ---------- equipment ----------
const EQUIPMENT = {
    inverter(g, w, d, h) {
        box(g, w, h, d, 0, 0, 0, MAT.white);
        [-1, 1].forEach(s => box(g, 0.04, h * 0.8, d * 0.9, s * (w / 2 + 0.02), h * 0.1, 0, MAT.dark));
        box(g, w * 0.4, h * 0.15, 0.02, 0, h * 0.7, d / 2 + 0.01, MAT.green);
    },
    battery(g, w, d, h) {                                      // stack of ~17 cm modules, green light on the top one
        const n = Math.max(1, Math.round(h / 0.17)), mh = h / n;
        for (let i = 0; i < n; i++) box(g, w, mh - 0.012, d, 0, i * mh, 0, MAT.white);
        box(g, w * 0.5, 0.025, 0.01, 0, h - mh / 2, d / 2 + 0.006, MAT.green);
    },
    panel(g, w, d, h) {
        box(g, w, h, d, 0, 0, 0, std(0x9ea7ad));
        box(g, 0.012, h * 0.94, 0.012, 0, h * 0.03, d / 2 + 0.006, MAT.dark);
        box(g, 0.03, 0.12, 0.03, w * 0.08, h * 0.5, d / 2 + 0.02, MAT.black);
    },
    convector(g, w, d, h) {                                    // wall-hung heater: white body with a dark grille
        box(g, w, h, d, 0, 0, 0, MAT.white);
        box(g, w * 0.9, h * 0.25, 0.01, 0, h * 0.62, d / 2 + 0.006, MAT.dark);
    },
    sensor(g, w, d, h) {
        box(g, w, h, d, 0, 0, 0, MAT.white);
        box(g, w * 0.3, h * 0.3, 0.01, 0, h * 0.4, d / 2 + 0.006, MAT.green);
    },
    camera(g, w, d, h) {                                       // dome / bullet camera: white body with a dark lens
        box(g, w, h, d, 0, 0, 0, MAT.white);
        box(g, w * 0.5, h * 0.5, 0.02, 0, h * 0.25, d / 2 + 0.01, MAT.black);
    },
    network(g, w, d, h) {                                      // switch / router: flat dark box with a row of port lights
        box(g, w, h, d, 0, 0, 0, MAT.dark);
        for (let i = 0; i < 8; i++) box(g, w * 0.07, h * 0.25, 0.01, (i - 3.5) * w * 0.11, h * 0.5, d / 2 + 0.006, MAT.green);
    },
};

// ---------- props (HallShape kind 'fixture' with a model) ----------
const FIXTURE = {
    extinguishers(g, w, d, h) {
        [-1, 0, 1].forEach(i => { cyl(g, 0.08, 0.5, i * 0.2, 0.25, 0, MAT.red); cyl(g, 0.03, 0.12, i * 0.2, 0.56, 0, MAT.black); });
    },
    hose_reel(g, w, d, h) {
        cyl(g, 0.28, 0.14, 0, 0.3, 0, MAT.teal, 'z');
        cyl(g, 0.12, 0.16, 0, 0.3, 0.01, MAT.dark, 'z');
    },
    tool_cart(g, w, d, h) {
        box(g, w, h * 0.85, d, 0, h * 0.1, 0, MAT.red);
        box(g, w * 0.95, 0.03, d * 0.95, 0, h * 0.95, 0, MAT.steel);
        [0.25, 0.45, 0.65].forEach(f => box(g, w * 0.9, 0.012, 0.01, 0, h * f, d / 2 + 0.006, MAT.black));
        [-1, 1].forEach(s => cyl(g, 0.05, 0.04, s * (w / 2 - 0.08), 0.05, 0, MAT.black, 'z'));
    },
    armchair(g, w, d, h) {
        box(g, w, h * 0.45, d, 0, 0.05, 0, MAT.burgundy);
        box(g, w, h * 0.55, d * 0.25, 0, h * 0.4, -d * 0.37, MAT.burgundy);
        [-1, 1].forEach(s => box(g, w * 0.15, h * 0.35, d * 0.8, s * (w / 2 - w * 0.075), h * 0.3, 0.02, MAT.burgundy));
    },
    tv(g, w, d, h) { box(g, w, h, 0.05, 0, 0, 0, MAT.black); box(g, w * 0.96, h * 0.92, 0.01, 0, h * 0.04, 0.03, MAT.screen); },
    locker(g, w, d, h) {
        box(g, w, h, d, 0, 0, 0, std(0x9fb0b8));
        for (let x = w / 3; x < w - 0.01; x += w / 3) box(g, 0.01, h * 0.96, 0.01, x - w / 2, h * 0.02, d / 2 + 0.006, MAT.dark);
    },
    drill_press(g, w, d, h) {
        box(g, w, 0.06, d, 0, 0, 0, MAT.dark);
        cyl(g, 0.04, h, 0, h / 2, -d * 0.3, MAT.steel);
        box(g, w * 0.7, h * 0.12, d * 0.5, 0, h * 0.82, -d * 0.1, MAT.green.clone ? std(0x3c6e47) : MAT.grey);
        box(g, w * 0.6, 0.04, d * 0.6, 0, h * 0.4, -d * 0.1, MAT.steel);
    },
    shelf(g, w, d, h) {
        [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([sx, sz]) => box(g, 0.05, h, 0.05, sx * (w / 2 - 0.03), 0, sz * (d / 2 - 0.03), MAT.steel));
        for (let y = 0.1; y < h; y += h / 4) box(g, w, 0.04, d, 0, y, 0, MAT.grey);
    },
};
export const FIXTURE_MODELS = Object.keys(FIXTURE);

function make(table, name, w, d, h) {
    const g = new THREE.Group();
    (table[name] || table.box || (() => {}))(g, w, d, h);
    return g;
}
export const buildMachine = (model, w, d, h) => make(MACHINE, model, w, d, h);
// Accessory ("инвентар") of a machine, e.g. a bar feeder, attached to one side of it (machine's own frame, front = +Z):
// 'left' = -X, 'right' = +X, 'back' = -Z, 'front' = +Z. w/d = machine footprint, len = how far it sticks out, aw = its width, ah = height.
// left/right are flush with the machine's rear edge; front/back are centred. userData.center = its centre (group-local).
export function buildAccessory(model, w, d, len, aw, ah, side = 'left') {
    const g = new THREE.Group(), inner = new THREE.Group();     // inner is built along X, then turned/placed per side
    if (model === 'bar_lathe') {
        box(inner, len, ah * 0.7, aw, 0, ah * 0.3, 0, MAT.white);
        box(inner, len, 0.05, aw * 1.04, 0, ah, 0, MAT.steel);
        [-len / 2 + 0.3, len / 2 - 0.3].forEach(x => box(inner, 0.1, ah * 0.3, 0.1, x, 0, 0, MAT.steel));
    } else {
        box(inner, len, ah, aw, 0, 0, 0, MAT.grey);
    }
    const at = { left: [-w / 2 - len / 2, -d / 2 + aw / 2, 0], right: [w / 2 + len / 2, -d / 2 + aw / 2, 0],
                 back: [0, -d / 2 - len / 2, Math.PI / 2], front: [0, d / 2 + len / 2, -Math.PI / 2] }[side] || [0, 0, 0];
    inner.position.set(at[0], 0, at[1]);
    inner.rotation.y = at[2];
    g.add(inner);
    g.userData.center = [inner.position.x, ah / 2, inner.position.z];
    return g;
}
export const buildEquipment = (kind, w, d, h) => make(EQUIPMENT, kind, w, d, h);
export const buildFixture = (model, w, d, h) => make(FIXTURE, model, w, d, h);

// ---------- textures (drawn on canvases, no image files) ----------
function tex(size, draw) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    draw(c.getContext('2d'), size);
    const t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = 4;
    return t;
}
export function makeTextures(dark) {
    const concrete = tex(512, (g, s) => {                       // grey polished concrete, 4 m per tile
        g.fillStyle = dark ? '#5d6066' : '#b4b1a8'; g.fillRect(0, 0, s, s);
        for (let i = 0; i < 5000; i++) {
            g.fillStyle = `rgba(${Math.random() < 0.5 ? '0,0,0' : '255,255,255'},${Math.random() * 0.06})`;
            g.fillRect(Math.random() * s, Math.random() * s, 2 + Math.random() * 14, 2 + Math.random() * 10);
        }
        g.strokeStyle = 'rgba(0,0,0,0.18)'; g.lineWidth = 2; g.strokeRect(0, 0, s, s);   // expansion joints
    });
    const panel = tex(256, (g, s) => {                          // 1 m sandwich panel: cream face, fine ribs, dark joint
        g.fillStyle = dark ? '#8d8b82' : '#efeadc'; g.fillRect(0, 0, s, s);
        g.strokeStyle = 'rgba(0,0,0,0.07)'; g.lineWidth = 2;
        for (let x = 0; x < s; x += s / 16) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, s); g.stroke(); }
        g.strokeStyle = 'rgba(0,0,0,0.3)'; g.lineWidth = 3; g.beginPath(); g.moveTo(1, 0); g.lineTo(1, s); g.stroke();
    });
    const ceiling = tex(512, (g, s) => {                        // 2.4 m: 4x4 tiles of 0.6 m, one LED panel
        g.fillStyle = dark ? '#7a7d82' : '#e9e6dc'; g.fillRect(0, 0, s, s);
        g.strokeStyle = 'rgba(0,0,0,0.22)'; g.lineWidth = 2;
        for (let i = 0; i <= 4; i++) { const p = i * s / 4; g.beginPath(); g.moveTo(p, 0); g.lineTo(p, s); g.moveTo(0, p); g.lineTo(s, p); g.stroke(); }
        g.fillStyle = '#ffffff'; g.fillRect(s / 4 + 6, s / 4 + 6, s / 4 - 12, s / 4 - 12);
        g.strokeStyle = '#9aa0a8'; g.strokeRect(s / 4 + 6, s / 4 + 6, s / 4 - 12, s / 4 - 12);
    });
    return { concrete, panel, ceiling };
}
