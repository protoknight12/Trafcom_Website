"""Camera pose from what the camera sees: the camera's position and height are known (its object on the hall plan), the user pairs points in the
image with known 3D points (machine / wall corners ...), and this finds yaw, tilt, roll and horizontal fov that project the points onto the clicks.

Same pinhole model as the 3D page's floorPoint(): hall axes x, z on the plan and y up (metres); yaw = degrees clockwise from +X on the plan,
tilt = degrees below the horizon, roll about the view axis; image (u, v) are fractions of the frame (0..1, v down). Optional pose[4] = k1,
one radial distortion term (x_d = x * (1 + k1 * r^2), r = 1 at the frame's half-width; < 0 barrel, > 0 pincushion), used by the solver only -
the saved pose (and the 3D page) stays pinhole. solve() can also nudge the camera position by up to +-0.5 m.
ponytail: pattern search over 4 numbers from a coarse grid - plenty for <= ~20 points; swap in scipy least_squares if it must be exact."""
import math
import random

RAD = math.pi / 180


def _basis(pose):
    """(forward, right, up) unit vectors of a camera with pose (yaw, tilt, roll, fov) in degrees."""
    yaw, tilt, roll = pose[0] * RAD, pose[1] * RAD, pose[2] * RAD
    f = (math.cos(tilt) * math.cos(yaw), -math.sin(tilt), math.cos(tilt) * math.sin(yaw))
    r = (-math.sin(yaw), 0.0, math.cos(yaw))
    up = (math.sin(tilt) * math.cos(yaw), math.cos(tilt), math.sin(tilt) * math.sin(yaw))
    r, up = ([r[i] * math.cos(roll) + up[i] * math.sin(roll) for i in range(3)], [-r[i] * math.sin(roll) + up[i] * math.cos(roll) for i in range(3)])
    return f, r, up


def project(cam, pose, pt, aspect):
    """3D point -> (u, v) or None when it is behind the camera. cam = (x, y, z); pose = (yaw, tilt, roll, fov[, k1]); aspect = frame height / width."""
    f, r, up = _basis(pose)
    d = [pt[i] - cam[i] for i in range(3)]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    zc = dot(d, f)
    if zc <= 1e-6:
        return None
    tf = math.tan(pose[3] * RAD / 2)
    x, y = dot(d, r) / zc / tf, dot(d, up) / zc / tf
    k = 1 + (pose[4] if len(pose) > 4 else 0.0) * (x * x + y * y)
    return 0.5 + x * k / 2, 0.5 - y * k / (2 * aspect)


def ray(pose, u, v, aspect):
    """Unit direction of the view ray through image point (u, v) (the inverse of project)."""
    f, r, up = _basis(pose)
    tf = math.tan(pose[3] * RAD / 2)
    a, b = (u - 0.5) * 2 * tf, (0.5 - v) * 2 * tf * aspect
    d = [f[i] + a * r[i] + b * up[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in d))
    return [x / n for x in d]


def floor_point(origin, d, max_dist=60.0):
    """Where the ray meets the floor (y = 0) as (x, z), or None (looking up / too far)."""
    if d[1] >= -1e-3 or origin[1] <= 0:
        return None
    s = -origin[1] / d[1]
    return None if s > max_dist else (origin[0] + s * d[0], origin[2] + s * d[2])


def triangulate(rays):
    """Point closest (least squares) to rays = [(origin, unit direction)]; returns (point, worst distance to a ray, all in front?)."""
    import numpy as np
    A, b = np.zeros((3, 3)), np.zeros(3)
    for o, d in rays:
        M = np.eye(3) - np.outer(d, d)
        A += M
        b += M @ np.asarray(o, float)
    try:
        p = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None, float('inf'), False                 # parallel rays
    worst, front = 0.0, True
    for o, d in rays:
        v = p - np.asarray(o, float)
        t = float(v @ np.asarray(d))
        front &= t > 0
        worst = max(worst, float(np.linalg.norm(v - t * np.asarray(d))))
    return (float(p[0]), float(p[1]), float(p[2])), worst, front


def _error(cam, pose, pairs, w, h):
    """Mean distance in pixels between projected and clicked points (a point behind the camera costs a lot)."""
    total = 0.0
    for pt, (u, v) in pairs:
        p = project(cam, pose, pt, h / w)
        total += 5 * w if p is None else math.hypot((p[0] - u) * w, (p[1] - v) * h)
    return total / len(pairs)


# params: yaw, tilt, roll, fov, k1, dx, dy, dz - (step scale, min, max); k1 and the position offsets are only free when asked for
_SCALE = (1, 1, 1, 1, 0.02, 0.05, 0.05, 0.05)
_LIMS = {1: (0.0, 90.0), 3: (20.0, 160.0), 4: (-0.6, 0.6), 5: (-0.5, 0.5), 6: (-0.5, 0.5), 7: (-0.5, 0.5)}


def solve(cam, pairs, w, h, distortion=False, move=False):
    """pairs = [((x, y, z), (u, v))]; returns {'yaw', 'tilt', 'roll', 'fov', 'k1', 'pos', 'error_px'} (3 pairs, +1 with distortion, +3 with move)."""
    need = 3 + distortion + 3 * move
    if len(pairs) < need:
        raise ValueError(f'Трябват поне {need} двойки точки (по-добре 5 и повече).')
    free = [0, 1, 2, 3] + [4] * distortion + [5, 6, 7] * move
    place = lambda p: ((cam[0] + p[5], cam[1] + p[6], cam[2] + p[7]), p[:5])
    err_of = lambda p: _error(*place(p), pairs, w, h)
    best = []
    for yaw in range(0, 360, 15):
        for tilt in range(0, 91, 10):
            for fov in (45, 70, 100):
                p = [yaw, tilt, 0.0, fov, 0.0, 0.0, 0.0, 0.0]
                best.append((err_of(p), p))
    best.sort(key=lambda x: x[0])
    rnd = random.Random(1)

    def descend(err, pose, free):
        for step in (10, 5, 2, 1, 0.5, 0.2, 0.1) * (2 if len(free) > 4 else 1):
            improved = True
            while improved:
                improved = False
                for i in free:
                    for sign in (1, -1):
                        trial = pose[:]
                        trial[i] += sign * step * _SCALE[i]
                        if i in _LIMS:
                            trial[i] = max(_LIMS[i][0], min(_LIMS[i][1], trial[i]))
                        e = err_of(trial)
                        if e < err - 1e-9:
                            err, pose, improved = e, trial, True
                if not improved and len(free) > 4:                  # correlated params (fov/k1/position) stall one-at-a-time moves: try random combined ones
                    for _ in range(60):
                        trial = pose[:]
                        for i in free:
                            trial[i] += rnd.uniform(-1, 1) * step * _SCALE[i]
                            if i in _LIMS:
                                trial[i] = max(_LIMS[i][0], min(_LIMS[i][1], trial[i]))
                        e = err_of(trial)
                        if e < err - 1e-9:
                            err, pose, improved = e, trial, True
                            break
        return err, pose

    results = []
    for err, pose in best[:6]:
        err, pose = descend(err, pose[:], free[:4])                  # pinhole first (finds the right basin), then the extras
        results.append(descend(err, pose, free) if len(free) > 4 else (err, pose))
    err, pose = min(results, key=lambda x: x[0])
    pos = place(pose)[0]
    return {'yaw': round(pose[0] % 360, 2), 'tilt': round(pose[1], 2), 'roll': round(pose[2], 2), 'fov': round(pose[3], 2), 'k1': round(pose[4], 3),
            'pos': [round(c, 3) for c in pos], 'error_px': round(err, 1)}
