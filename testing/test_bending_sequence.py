"""bending.sequence on hand-built unfolds. Run: python -m testing.test_bending_sequence"""
from bending.sequence import steps_from_unfold

# Z profile: lines at 20 and 60 on a 100 mm flat, second fold goes the other way -> flip
u = {'flat_length': 100.0, 'bends': [{'angle': 90, 'line': 20.0, 'dir': 'up', 'kind': 'bend'},
                                     {'angle': 90, 'line': 60.0, 'dir': 'down', 'kind': 'bend'}]}
s = steps_from_unfold(u)
assert [(x['seq'], x['x_mm'], x['flip']) for x in s] == [(1, 80.0, False), (2, 40.0, True)]
# U profile: both up -> no flip; hem is annotated
u2 = {'flat_length': 100.0, 'bends': [{'angle': 180, 'line': 5.0, 'dir': 'up', 'kind': 'hem'},
                                      {'angle': 90, 'line': 40.0, 'dir': 'up', 'kind': 'bend'}]}
s2 = steps_from_unfold(u2)
assert [x['flip'] for x in s2] == [False, False] and 'Подгъвка' in s2[0]['notes'] and s2[1]['notes'] is None
print('ok')
