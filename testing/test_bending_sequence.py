"""bending.sequence on hand-built unfolds. Run: python -m testing.test_bending_sequence"""
from bending.sequence import steps_from_unfold

# Z profile on a 100 mm flat: lines at 20 and 60. Short side to the gauge: 20 from the left end, then 40 (the right side is 40 too -> left end first)
u = {'flat_length': 100.0, 'bends': [{'angle': 90, 'line': 20.0, 'dir': 'up', 'kind': 'bend'},
                                     {'angle': 90, 'line': 60.0, 'dir': 'down', 'kind': 'bend'}]}
s = steps_from_unfold(u)
assert [(x['seq'], x['x_mm'], x['flip'], x['back_end']) for x in s] == [(1, 20.0, False, 'left'), (2, 40.0, True, 'right')]
assert 'Завъртете' in s[1]['notes'] and 'Късата' in s[0]['notes']
# the long part is always towards the operator: X never exceeds the other side
for x in s:
    assert x['x_mm'] <= 100 - x['x_mm']
# U profile 100 mm, flanges 20: both bends 20 from their ends, both up -> no turn over but an end-for-end turn
u2 = {'flat_length': 100.0, 'bends': [{'angle': 180, 'line': 5.0, 'dir': 'up', 'kind': 'hem'},
                                      {'angle': 90, 'line': 90.0, 'dir': 'up', 'kind': 'bend'}]}
s2 = steps_from_unfold(u2)
assert [x['flip'] for x in s2] == [False, False] and 'Подгъвка' in s2[0]['notes'] and [x['x_mm'] for x in s2] == [5.0, 10.0]
assert [x['back_end'] for x in s2] == ['left', 'right']
print('ok')
