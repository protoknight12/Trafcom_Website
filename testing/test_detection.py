"""detection.decode(): YOLOX raw output -> labelled boxes as fractions of the frame (people / vehicles only, NMS per class)."""
import numpy as np

import detection


def raw_with(preds):
    """preds: [(cell index, cx_offset, w_px, cls, score)] - everything else is background."""
    raw = np.zeros((8400, 85), np.float32)
    raw[:, 4] = 0.01
    for i, off, wpx, cls, sc in preds:
        raw[i, 0:2] = off
        raw[i, 2:4] = np.log(wpx / 8)           # stride-8 cell: exp(w) * 8 = wpx
        raw[i, 4] = 1.0
        raw[i, 5 + cls] = sc
    return raw


def test_person_box():
    res = detection.decode(raw_with([(100, 0.5, 80, 0, 0.9)]), 1.0, 640, 640)
    assert len(res) == 1 and res[0]['label'] == 'човек' and res[0]['score'] == 0.9
    assert abs(res[0]['box'][0] - 0.19375) < 1e-3 and res[0]['box'][1] == 0       # clipped at the frame edge


def test_other_classes_and_weak_scores_are_ignored():
    # 56 = chair (not in CLASSES), and a person below the threshold
    assert detection.decode(raw_with([(100, 0.5, 80, 56, 0.9), (200, 0.5, 80, 0, 0.2)]), 1.0, 640, 640) == []


def test_nms_merges_overlapping_boxes_of_one_class():
    res = detection.decode(raw_with([(100, 0.5, 80, 2, 0.9), (101, 0.5, 80, 2, 0.8)]), 1.0, 640, 640, nms_iou=0.3)
    assert [r['label'] for r in res] == ['кола']


def test_custom_model_keeps_all_its_classes():
    raw = np.zeros((8400, 7), np.float32)            # 2 custom classes
    raw[:, 4] = 0.01
    raw[100, 0:2], raw[100, 2:4], raw[100, 4], raw[100, 6] = 0.5, np.log(10), 1.0, 0.9
    res = detection.decode(raw, 1.0, 640, 640, names=['кар', 'палет'])
    assert [r['label'] for r in res] == ['палет']


def test_tiles_cover_the_frame_with_overlap():
    assert detection._tiles(1280, 720) == []                                       # small enough: the full pass is enough
    tiles = detection._tiles(1920, 1080)
    assert len(tiles) == 6 and all(tw <= detection.TILE and th <= detection.TILE for _, _, tw, th in tiles)
    assert max(x + tw for x, _, tw, _ in tiles) == 1920 and max(y + th for _, y, _, th in tiles) == 1080
    assert len(detection._tiles(3840, 2160)) == 15


def test_merge_drops_the_half_box_a_tile_edge_cut():
    whole = {'label': 'човек', 'score': 0.9, 'box': [0.40, 0.30, 0.50, 0.60]}
    half = {'label': 'човек', 'score': 0.7, 'box': [0.40, 0.30, 0.45, 0.60]}       # same person cut by a piece's border
    other = {'label': 'човек', 'score': 0.6, 'box': [0.80, 0.30, 0.90, 0.60]}
    car = {'label': 'кола', 'score': 0.5, 'box': [0.40, 0.30, 0.50, 0.60]}         # another label on the same spot stays
    assert detection._merge([half, car, other, whole]) == [whole, other, car]


def test_detect_maps_piece_boxes_back_to_the_frame(monkeypatch):
    import io
    from PIL import Image
    seen = []

    def fake(img, min_score):
        seen.append(img.size)
        # a person filling the middle of every image it is shown (the full pass and each piece)
        return [{'label': 'човек', 'score': 0.9, 'box': [0.4, 0.4, 0.6, 0.6]}]
    monkeypatch.setattr(detection, '_infer', fake)
    buf = io.BytesIO()
    Image.new('RGB', (1920, 1080)).save(buf, 'JPEG')
    res = detection.detect(buf.getvalue())
    assert len(seen) == 7 and seen[0] == (1920, 1080)                              # full pass + 6 pieces
    assert any(abs(r['box'][0] - 0.4) < 1e-3 for r in res)                          # the full-pass box kept
    assert len(res) > 1                                                             # pieces found their own, different spots
