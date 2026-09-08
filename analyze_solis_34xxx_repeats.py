"""
Repeatability analysis for probe_solis_34xxx.py's dump
(solis_34xxx_probe_data.json) - looks for a repeating "record" structure in
the 34xxx range by testing every candidate offset (record length) and
counting how many address pairs (a, a+offset) hold either the EXACT same
value (config/marker-like fields, e.g. the 43605 sentinel) or a CLOSE-but-
not-identical value (telemetry-like fields that vary a little between two
otherwise-identical records, e.g. cell voltage between BMS port 1 and 2).

Explicit ask: "направи анализ за повтаремост на близки данни с някакво
отместване". Confirms the already-known offset=25 structure (the two
25-register battery-port groups at 34346/34371) as a sanity check, and
looks for any OTHER offset that scores just as well - a strong hit at a
new offset would mean an as-yet-unmapped repeating record (candidate home
for a per-port SOC we haven't found yet).

    python analyze_solis_34xxx_repeats.py
"""
import io
import json
import sys
from collections import defaultdict

MAX_OFFSET = 300
CLOSE_REL_TOL = 0.05  # values within 5% of each other count as "close"

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open('solis_34xxx_probe_data.json', encoding='utf-8') as f:
    raw = json.load(f)

devices = {int(k): {int(a): v for a, v in regs.items()} for k, regs in raw.items()}


def analyze(regs):
    exact = defaultdict(list)
    close = defaultdict(list)
    addrs = sorted(regs)
    for a in addrs:
        v1 = regs[a]
        for delta in range(1, MAX_OFFSET + 1):
            b = a + delta
            if b not in regs:
                continue
            v2 = regs[b]
            if v1 == v2:
                exact[delta].append((a, v1, b, v2))
            else:
                rel = abs(v1 - v2) / max(v1, v2)
                if rel <= CLOSE_REL_TOL:
                    close[delta].append((a, v1, b, v2))
    return exact, close


per_device_scores = {}
for dev_id, regs in devices.items():
    exact, close = analyze(regs)
    scores = defaultdict(int)
    for delta in set(exact) | set(close):
        scores[delta] = len(exact[delta]) + len(close[delta])
    per_device_scores[dev_id] = (scores, exact, close)

print('=== Top offsets by device (exact + close match count) ===')
for dev_id, (scores, exact, close) in per_device_scores.items():
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:15]
    print(f'\nDevice {dev_id}:')
    for delta, score in top:
        print(f'  offset {delta}: score={score} (exact={len(exact[delta])}, close={len(close[delta])})')

# Offsets that score well in BOTH devices independently - much stronger
# evidence of a real repeating record structure, not coincidence.
dev_ids = list(devices.keys())
if len(dev_ids) == 2:
    s1 = per_device_scores[dev_ids[0]][0]
    s2 = per_device_scores[dev_ids[1]][0]
    combined = {delta: s1.get(delta, 0) + s2.get(delta, 0)
                for delta in set(s1) | set(s2) if s1.get(delta, 0) > 0 and s2.get(delta, 0) > 0}
    top_combined = sorted(combined.items(), key=lambda kv: -kv[1])[:15]
    print('\n=== Top offsets confirmed in BOTH devices ===')
    for delta, score in top_combined:
        print(f'  offset {delta}: combined score={score} '
              f'(dev{dev_ids[0]}={s1.get(delta,0)}, dev{dev_ids[1]}={s2.get(delta,0)})')

    for dev_id in dev_ids:
        print('\n=== Detail for the best few offsets (device {}) ==='.format(dev_id))
        exact1, close1 = per_device_scores[dev_id][1], per_device_scores[dev_id][2]
        for delta, _ in top_combined[:5]:
            print(f'\n--- offset {delta} ---')
            for a, v1, b, v2 in sorted(exact1[delta])[:10]:
                print(f'  EXACT  {a}={v1}  <->  {b}={v2}')
            for a, v1, b, v2 in sorted(close1[delta])[:10]:
                print(f'  CLOSE  {a}={v1}  <->  {b}={v2}  (diff={abs(v1-v2)})')
