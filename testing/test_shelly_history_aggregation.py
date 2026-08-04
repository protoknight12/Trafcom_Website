"""
_aggregate_shelly_history(): arithmetic mean for every field except
*_total_act_energy, which is summed into one kWh figure (the historical-log
feature on /admin/power's device page - see app.py's admin_power_history()).
Run: python -m testing.test_shelly_history_aggregation
"""
from app import _aggregate_shelly_history

# Empty window -> no rows, zero energy, no channels.
assert _aggregate_shelly_history([]) == {'count': 0, 'energy_kwh': 0.0, 'channels': {}}

rows = [
    {'timestamp': 1000, 'a_avg_voltage': 230.0, 'a_avg_current': 1.0, 'a_total_act_energy': 100.0,
     'b_avg_voltage': 231.0, 'b_total_act_energy': 50.0},
    {'timestamp': 1060, 'a_avg_voltage': 232.0, 'a_avg_current': 2.0, 'a_total_act_energy': 200.0,
     'b_avg_voltage': 233.0, 'b_total_act_energy': 60.0},
]
result = _aggregate_shelly_history(rows)
assert result['count'] == 2
# Energy fields are summed (interval consumption), converted Wh -> kWh:
# (100 + 200 + 50 + 60) / 1000 = 0.41
assert result['energy_kwh'] == 0.41
# Everything else is the arithmetic mean, grouped by its channel prefix.
assert result['channels']['a']['avg_voltage'] == 231.0
assert result['channels']['a']['avg_current'] == 1.5
assert result['channels']['b']['avg_voltage'] == 232.0
assert 'a_total_act_energy' not in result['channels'].get('a', {})

print('test_shelly_history_aggregation: OK')
