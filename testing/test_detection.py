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
