"""Object detection for the camera frames: YOLOX-s (Apache-2.0, COCO) on onnxruntime, CPU.
detect(jpeg_bytes) -> [{'label', 'score', 'box': [x1, y1, x2, y2]}] with the box as fractions (0..1) of the frame."""
import io
import json
import os
import threading

import numpy as np
from PIL import Image

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'yolox_s.onnx')
# optional second model trained on the shop's own objects (forklifts, pallets, parts ...): a YOLOX export at 640x640 plus the class names
# (a JSON list, index = class id) - see docs/CUSTOM_OBJECTS.md. Its detections are added to the COCO ones.
CUSTOM_PATH = os.path.join(os.path.dirname(MODEL_PATH), 'custom.onnx')
CUSTOM_NAMES_PATH = os.path.join(os.path.dirname(MODEL_PATH), 'custom.json')
SIZE = 640
# COCO class id -> Bulgarian label; only what the shop cares about (people and vehicles)
CLASSES = {0: 'човек', 1: 'колело', 2: 'кола', 3: 'мотор', 5: 'автобус', 7: 'камион'}

_sessions = {}
_lock = threading.Lock()


def available():
    return os.path.exists(MODEL_PATH)


def custom_available():
    return os.path.exists(CUSTOM_PATH) and os.path.exists(CUSTOM_NAMES_PATH)


def _get_session(path=MODEL_PATH):
    with _lock:
        if path not in _sessions:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)       # leave the rest to the web app
            _sessions[path] = ort.InferenceSession(path, opts, providers=['CPUExecutionProvider'])
    return _sessions[path]


def _grids():
    out = []
    for stride in (8, 16, 32):
        n = SIZE // stride
        gx, gy = np.meshgrid(np.arange(n), np.arange(n))
        out.append((np.stack((gx, gy), 2).reshape(-1, 2), np.full((n * n, 1), stride)))
    return np.concatenate([g for g, _ in out]), np.concatenate([s for _, s in out])


_GRID, _STRIDE = _grids()


def _nms(boxes, scores, thr):
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1, yy1 = np.maximum(boxes[i, 0], boxes[rest, 0]), np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2, yy2 = np.minimum(boxes[i, 2], boxes[rest, 2]), np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
        order = rest[inter / (area(boxes[i]) + area(boxes[rest]) - inter + 1e-9) <= thr]
    return keep


def decode(raw, ratio, w, h, min_score=0.4, nms_iou=0.5, names=None):
    """YOLOX raw output (8400, 5 + classes) -> detections; ratio = letterbox scale, w/h = original frame size.
    names = class names of a custom model (index = class id, all kept); None = the COCO model, only CLASSES kept."""
    names = {i: n for i, n in enumerate(names)} if names is not None else CLASSES
    o = raw.copy()
    o[:, :2] = (o[:, :2] + _GRID) * _STRIDE
    o[:, 2:4] = np.exp(o[:, 2:4]) * _STRIDE
    cls_scores = o[:, 5:] * o[:, 4:5]
    cls = cls_scores.argmax(1)
    score = cls_scores[np.arange(len(o)), cls]
    ok = (score >= min_score) & np.isin(cls, list(names))
    o, cls, score = o[ok], cls[ok], score[ok]
    if not len(o):
        return []
    xyxy = np.stack([o[:, 0] - o[:, 2] / 2, o[:, 1] - o[:, 3] / 2, o[:, 0] + o[:, 2] / 2, o[:, 1] + o[:, 3] / 2], 1) / ratio
    res = []
    for c in np.unique(cls):
        idx = np.where(cls == c)[0]
        for k in _nms(xyxy[idx], score[idx], nms_iou):
            b = xyxy[idx[k]]
            res.append({'label': names[int(c)], 'score': round(float(score[idx[k]]), 3),
                        'box': [round(float(max(0, min(1, v))), 4) for v in (b[0] / w, b[1] / h, b[2] / w, b[3] / h)]})
    return sorted(res, key=lambda d: -d['score'])


# A frame bigger than this is also cut into overlapping TILE-sized pieces (source pixels), each run through the models on its own, so small
# objects aren't lost when a 4K frame is squeezed to 640x640. 0 = off. Cost: one model pass per piece (a 1080p frame = 6, a 4K one = 15).
TILE = int(os.environ.get('DETECT_TILE', '960'))
TILE_OVERLAP = 0.2


def _tiles(w, h):
    """[(x, y, tw, th)] overlapping pieces covering a w x h frame, or [] when it is small enough for the one full pass."""
    if not TILE or max(w, h) <= TILE * 1.5:
        return []

    def starts(n):
        return [0] if n <= TILE else list(range(0, n - TILE, int(TILE * (1 - TILE_OVERLAP)))) + [n - TILE]
    return [(x, y, min(TILE, w), min(TILE, h)) for y in starts(h) for x in starts(w)]


def _merge(items, iou_thr=0.5, inside=0.8):
    """One box per object after the full pass and the pieces all reported it: a lower-scored box of the same label is dropped when it
    overlaps a kept one (IoU) or lies mostly inside it - a piece's edge cuts objects, and the half box is inside the whole one.
    ponytail: two people standing almost on top of each other can lose one; fine for counting presence."""
    keep = []
    for d in sorted(items, key=lambda d: -d['score']):
        for k in keep:
            if k['label'] != d['label']:
                continue
            a, b = k['box'], d['box']
            inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            area = lambda r: max(1e-9, (r[2] - r[0]) * (r[3] - r[1]))
            if inter / (area(a) + area(b) - inter) > iou_thr or inter / min(area(a), area(b)) > inside:
                break
        else:
            keep.append(d)
    return keep


def _infer(img, min_score):
    """COCO (+ custom) detections of one image, boxes as fractions of that image."""
    w, h = img.size
    ratio = min(SIZE / w, SIZE / h)
    img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))))
    canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)
    canvas[:img.height, :img.width] = np.asarray(img)
    x = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32))      # YOLOX takes BGR, 0..255, no normalisation
    res = decode(_get_session().run(None, {'images': x})[0][0], ratio, w, h, min_score)
    if custom_available():
        with open(CUSTOM_NAMES_PATH, encoding='utf-8') as fh:
            names = json.load(fh)
        res += decode(_get_session(CUSTOM_PATH).run(None, {'images': x})[0][0], ratio, w, h, min_score, names=names)
    return res


def detect(jpeg, min_score=0.4):
    img = Image.open(io.BytesIO(jpeg)).convert('RGB')
    w, h = img.size
    res = _infer(img, min_score)
    for x, y, tw, th in _tiles(w, h):
        for d in _infer(img.crop((x, y, x + tw, y + th)), min_score):
            b = d['box']
            d['box'] = [round(v, 4) for v in ((x + b[0] * tw) / w, (y + b[1] * th) / h, (x + b[2] * tw) / w, (y + b[3] * th) / h)]
            res.append(d)
    return _merge(res)
