"""bending.unfold core, hand-computed examples (L, U, Z; t = 1 and 3 mm; R = t). Run: python -m testing.test_bending_unfold"""
import math

from bending.unfold import bd_table, k_factor, unfold_section


def unfold(outs, angles, signs, t, strategy):
    n = len(outs)
    return unfold_section(outs, angles, [t] * (n - 1), ['bend'] * (n - 1), signs, t, strategy,
                          [str(i + 1) for i in range(n)], [False] * n)


def close(a, b, tol=1e-3):
    assert abs(a - b) <= tol, (a, b)


# t=1, R=1, K=0.4, 90 deg: setback 2, BA = pi/2*1.4 = 2.1991, BD = 4 - 2.1991 = 1.8009
u = unfold([50, 30], [90], [1], 1, k_factor(0.4))
close(u['bends'][0]['allowance'], 2.1991); close(u['bends'][0]['deduction'], 1.8009)
close(u['flat'], 80 - 1.8009)                      # L
# t=3, R=3, K=0.4: setback 6, BA = pi/2*4.2 = 6.5973, BD = 5.4027
close(unfold([100, 60], [90], [1], 3, k_factor(0.4))['flat'], 160 - 5.4027)
# U (two bends) and Z: flat = sum - 2*BD
close(unfold([30, 60, 30], [90, 90], [1, 1], 1, k_factor(0.4))['flat'], 120 - 2 * 1.8009)
z = unfold([20, 40, 20], [90, 90], [1, -1], 3, k_factor(0.4))
close(z['flat'], 80 - 2 * 5.4027)
assert [b['dir'] for b in z['bends']] == ['up', 'down']
# BD-table strategy giving the same BD reproduces the K-factor result; missing entry is an error
bd = bd_table(lambda a, r, t: 1.8009)
close(unfold([50, 30], [90], [1], 1, bd)['flat'], 80 - 1.8009)
try:
    unfold([50, 30], [90], [1], 1, bd_table(lambda a, r, t: None))
    raise SystemExit('expected ValueError')
except ValueError:
    pass
# flange shorter than its own setback is rejected
try:
    unfold([3, 30], [90], [1], 3, k_factor(0.4))
    raise SystemExit('expected ValueError')
except ValueError:
    pass
print('ok')
