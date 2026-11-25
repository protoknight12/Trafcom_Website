from datetime import datetime, date, timedelta

import hvac

MON = datetime(2026, 11, 23, 8, 0)  # Monday
SCHED = [(0, 420, 1020, 21.0), (1, 420, 1020, 21.0)]


def test_decide_heat_deadband():
    assert hvac.decide('heat', 20.4, 21, 0.5, False) is True
    assert hvac.decide('heat', 21.6, 21, 0.5, True) is False
    assert hvac.decide('heat', 21.0, 21, 0.5, True) is None


def test_decide_cool_mirrored():
    assert hvac.decide('cool', 24.6, 24, 0.5, False) is True
    assert hvac.decide('cool', 23.4, 24, 0.5, True) is False
    assert hvac.decide('cool', 24.0, 24, 0.5, True) is None


def test_decide_no_target_off():
    assert hvac.decide('heat', 10, None, 0.5, True) is False
    assert hvac.decide('off', 10, 21, 0.5, True) is False


def test_schedule_interval_and_lowered():
    assert hvac.scheduled_target(SCHED, 16.0, MON, set()) == (21.0, MON.replace(hour=17))
    t, nxt = hvac.scheduled_target(SCHED, 16.0, MON.replace(hour=18), set())
    assert (t, nxt) == (16.0, datetime(2026, 11, 24, 7, 0))


def test_schedule_equal_temps_not_a_change():
    s = [(0, 420, 720, 21.0), (0, 720, 1020, 21.0)]
    assert hvac.scheduled_target(s, 16.0, MON, set())[1] == MON.replace(hour=17)


def test_holiday_uses_sunday():
    s = [(6, 600, 900, 19.0)]
    t, nxt = hvac.scheduled_target(s, 16.0, MON, {MON.date()})
    assert t == 16.0 and nxt == MON.replace(hour=10)
    t, nxt = hvac.scheduled_target(s, 16.0, MON.replace(hour=11), {MON.date()})
    assert (t, nxt) == (19.0, MON.replace(hour=15))


def test_override_capped_24h():
    t, nxt = hvac.scheduled_target([], None, MON, set())
    assert (t, nxt) == (None, None)
    assert hvac.override_until(nxt, MON) == MON + timedelta(hours=24)
    assert hvac.override_until(MON + timedelta(hours=3), MON) == MON + timedelta(hours=3)
    assert hvac.override_until(MON + timedelta(days=3), MON) == MON + timedelta(hours=24)


def test_effective_target_hold_and_expired():
    until = MON + timedelta(hours=1)
    assert hvac.effective_target(21, None, 25, until, False, MON) == (25, 'override')
    assert hvac.effective_target(21, None, 25, until, False, MON + timedelta(hours=2)) == (21, 'schedule')
    assert hvac.effective_target(21, None, 25, None, True, MON) == (25, 'hold')
    assert hvac.effective_target(21, None, None, None, False, MON) == (21, 'schedule')


def test_clamp():
    assert hvac.clamp('heat', 2) == 5.0
    assert hvac.clamp('heat', 40) == 30.0
    assert hvac.clamp('cool', 10) == 18.0
    assert hvac.clamp('cool', 25) == 25
    assert hvac.clamp('off', 99) == 99


def test_validate_intervals():
    assert hvac.validate_intervals([(0, 1320, 360)])
    assert hvac.validate_intervals([(0, 420, 720), (0, 600, 900)])
    assert hvac.validate_intervals([(0, -5, 60)])
    assert hvac.validate_intervals([(0, 0, 1441)])
    assert hvac.validate_intervals([(0, 420, 720), (0, 720, 1020), (1, 420, 720)]) is None
