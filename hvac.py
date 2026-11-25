"""Чиста логика на термостатите (отопление/охлаждане). Без Flask, само stdlib.

Часовете са наивно локално време; ден от седмицата Пон=0 (datetime.weekday()).
"""
from datetime import datetime, timedelta

HEAT_RANGE = (5.0, 30.0)
COOL_RANGE = (18.0, 30.0)
OVERRIDE_MAX_H = 24


def clamp(season, temp):
    if season == 'heat':
        lo, hi = HEAT_RANGE
    elif season == 'cool':
        lo, hi = COOL_RANGE
    else:
        return temp
    return max(lo, min(hi, temp))


def _day_intervals(intervals, d, holidays):
    wd = 6 if d in holidays else d.weekday()
    return [(s, e, t) for w, s, e, t in intervals if w == wd]


def _target_at(intervals, lowered, dt, holidays):
    m = dt.hour * 60 + dt.minute
    for s, e, t in _day_intervals(intervals, dt.date(), holidays):
        if s <= m < e:
            return t
    return lowered


def scheduled_target(intervals, lowered, now, holidays):
    """(цел сега, местно време на следващата промяна на целта | None)."""
    cur = _target_at(intervals, lowered, now, holidays)
    cands = set()
    for off in range(8):
        d = now.date() + timedelta(days=off)
        base = datetime(d.year, d.month, d.day)
        cands.add(base)  # смяна на деня (празник/делник)
        for s, e, _ in _day_intervals(intervals, d, holidays):
            cands.add(base + timedelta(minutes=s))
            cands.add(base + timedelta(minutes=e))
    for c in sorted(cands):
        if now < c <= now + timedelta(days=7) and _target_at(intervals, lowered, c, holidays) != cur:
            return cur, c
    return cur, None


def effective_target(sched_temp, next_change, override_temp, override_until, hold, now):
    if override_temp is not None:
        if hold:
            return override_temp, 'hold'
        if override_until is not None and override_until > now:
            return override_temp, 'override'
    return sched_temp, 'schedule'


def override_until(next_change, now):
    cap = now + timedelta(hours=OVERRIDE_MAX_H)
    return min(next_change, cap) if next_change else cap


def decide(season, temp, target, hyst, is_on):
    """True = включи, False = изключи, None = остави както е."""
    if target is None or season not in ('heat', 'cool'):
        return False
    if season == 'cool':
        temp, target = -temp, -target
    if temp < target - hyst:
        return True
    if temp > target + hyst:
        return False
    return None


def validate_intervals(rows):
    """rows: (weekday, start_min, end_min, ...). Текст на грешка на български или None."""
    by_day = {}
    for r in rows:
        wd, s, e = r[0], r[1], r[2]
        if not (0 <= s <= 1440 and 0 <= e <= 1440):
            return 'Часът трябва да е между 00:00 и 24:00.'
        if e <= s:
            return 'Краят трябва да е след началото (интервалът не може да минава през полунощ).'
        by_day.setdefault(wd, []).append((s, e))
    for lst in by_day.values():
        lst.sort()
        for (_, e1), (s2, _) in zip(lst, lst[1:]):
            if s2 < e1:
                return 'Интервалите в един ден не могат да се припокриват.'
    return None
