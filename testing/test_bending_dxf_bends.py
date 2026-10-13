from bending.dxf_bends import steps_from_bend_lines

# 100 mm flat: bends at 20 (up), 50 (down), 80 (up, 45 deg). Outer first: both 20s, then the middle one.
lines = [('BEND_UP', (20, 0), (20, 30)), ('BEND_DOWN', (50, 0), (50, 30)), ('BEND_UP_45', (80, 0), (80, 30))]
s = steps_from_bend_lines(lines, (0, 0), (100, 30))
assert [(x['seq'], x['angle_deg'], x['x_mm'], x['back_end'], x['flip']) for x in s] == [
    (1, 90, 20, 'left', False), (2, 45, 20, 'right', False), (3, 90, 50, 'left', True)]
assert 'проверете' in s[0]['notes'] and 'Завъртете' in s[1]['notes']
# horizontal bend lines work too; a bend 10 from the end of a 50 flat has X=10 (short side to the gauge)
s = steps_from_bend_lines([('BEND_UP', (0, 40), (40, 40))], (0, 0), (40, 50))
assert s[0]['x_mm'] == 10 and s[0]['back_end'] == 'right'
assert steps_from_bend_lines([], (0, 0), (1, 1)) == []
print('ok')

# the model's inner radius travels in the layer name: BEND_<dir>_<angle>_R<radius>
s = steps_from_bend_lines([('BEND_DOWN_90_R2.5', (20, 0), (20, 30)), ('BEND_UP_45', (60, 0), (60, 30))], (0, 0), (100, 30))
assert [x.get('inner_radius_mm') for x in sorted(s, key=lambda x: x['seq'])] == [2.5, None] and sorted(x['angle_deg'] for x in s) == [45, 90]
print('ok radius')
