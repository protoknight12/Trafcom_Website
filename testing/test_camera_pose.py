"""camera_pose.solve(): recovers a known pose from points projected with project()."""
import camera_pose as cp

CAM = (10.0, 4.0, 5.0)
POINTS = [(14, 0, 3), (16, 0, 8), (12, 1.8, 9), (18, 0, 5), (15, 2.5, 6), (13, 0, 2)]


def test_recovers_pose():
    true = (35.0, 40.0, 0.0, 85.0)
    w, h = 1920, 1080
    pairs = []
    for p in POINTS:
        uv = cp.project(CAM, true, p, h / w)
        assert uv, p                       # may lie a little outside the frame - the maths is the same
        pairs.append((p, uv))
    got = cp.solve(CAM, pairs, w, h)
    assert got['error_px'] < 1.0
    assert abs(got['yaw'] - 35) < 1.5 and abs(got['tilt'] - 40) < 1.5 and abs(got['fov'] - 85) < 3


def test_needs_three_pairs():
    try:
        cp.solve(CAM, [((1, 0, 1), (0.5, 0.5))] * 2, 1920, 1080)
    except ValueError:
        return
    raise AssertionError('expected ValueError')


def test_triangulation_of_one_object_seen_by_two_cameras():
    obj = (14.0, 1.0, 6.0)
    cams = [((10.0, 4.0, 5.0), (30.0, 35.0, 0.0, 90.0)), ((18.0, 4.0, 10.0), (230.0, 30.0, 0.0, 90.0))]
    rays = []
    for pos, pose in cams:
        uv = cp.project(pos, pose, obj, 9 / 16)
        assert uv, pos
        rays.append((pos, cp.ray(pose, uv[0], uv[1], 9 / 16)))
    p, worst, front = cp.triangulate(rays)
    assert front and worst < 1e-6 and all(abs(a - b) < 1e-6 for a, b in zip(p, obj))
    assert cp.floor_point(*rays[0]) is not None


def test_recovers_distortion_and_position_offset():
    true, k1, off = (35.0, 40.0, 0.0, 85.0), -0.15, (0.3, -0.2, 0.25)
    real = tuple(c + o for c, o in zip(CAM, off))
    w, h = 1920, 1080
    pts = POINTS + [(11, 0, 9), (17, 2, 3)]
    pairs = [(p, cp.project(real, true + (k1,), p, h / w)) for p in pts]
    plain = cp.solve(CAM, pairs, w, h)
    got = cp.solve(CAM, pairs, w, h, distortion=True, move=True)
    assert got['error_px'] < plain['error_px'] and got['error_px'] < 3.0
    assert all(abs(a - b) <= 0.5 + 1e-9 for a, b in zip(got['pos'], CAM))
