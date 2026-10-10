"""Fibonacci story points, from tools/story_point_calendar.py.

A story point measures a whole ACTIVITY, not a sitting. That is why an activity's points
sit on its first sitting and every later sitting carries 0 — splitting 3 points over 8
sittings gives fractions that do not add back.
"""

_FIBONACCI_STORY_POINTS = [
    (0,   20,   40,    1,  2,  "1"),
    (1,   60,   120,   3,  3,  "1"),
    (2,   240,  360,   6,  12, "2-4"),
    (3,   480,  600,   12, 15, "4-5"),
    (5,   720,  840,   18, 21, "6-7"),
]

_FIBONACCI_SCALE = [1, 2, 3, 5, 8]        # allowed user-adjustable story-point values


def _ceil_div(numer, denom):
    """Integer ceiling division without importing math."""
    if denom <= 0:
        return 0
    return int(numer // denom) + (1 if numer % denom else 0)

def activity_count(minutes, act_min, act_max):
    """How many activity atoms a duration actually breaks into, inside its band's range.

    The band gives a range (SP3 = 12-15) rather than one number, because a 480-minute
    SP3 and a 600-minute SP3 are not the same amount of work. Take the fewest atoms that
    keep each one at or under the 40-minute ceiling, hold it inside the band's range,
    then cap it so no atom is driven under the 30-minute floor. The result is that a
    480-min task gets 12 x 40 and a 600-min task gets 15 x 40 — both exactly on the
    workbook's numbers, which is what minutes/40 was always describing.
    """
    try:
        mins = max(0.0, float(minutes or 0))
    except (TypeError, ValueError):
        mins = 0.0
    if mins <= 0:
        return max(1, int(act_min))
    n = _ceil_div(mins, _ACTIVITY_CEILING_MINUTES)          # each atom <= 40 min
    n = min(max(n, int(act_min)), int(act_max))             # stay inside the band
    by_floor = int(mins // _ACTIVITY_FLOOR_MINUTES)         # each atom >= 30 min
    return max(1, min(n, by_floor) if by_floor else 1)

def derive_story_point(duration_minutes):
    """Map a task duration (minutes) to its Fibonacci story point + expected activity
    count + working-session span, via upper-bound duration-band thresholds. Deterministic:
    the smallest story point whose band max >= duration wins. Caps at the largest defined
    single-task band (SP5) and flags durations that overflow it as split candidates."""
    try:
        mins = max(0.0, float(duration_minutes))
    except (TypeError, ValueError):
        mins = 0.0

    def _pack(sp, lo, hi, act_lo, act_hi, sessions, overflow):
        return {
            "story_point":              sp,
            "duration_minutes":         round(mins, 2),
            "band_min_minutes":         lo,
            "band_max_minutes":         hi,
            "expected_activities":      activity_count(mins, act_lo, act_hi),
            "expected_activities_min":  act_lo,
            "expected_activities_max":  act_hi,
            "session_span":             sessions,
            "sessions_needed":          max(1, _ceil_div(mins, _WORKING_SESSION_MINUTES)),
            "derivation":               "duration_band_upper_bound",
            "adjustable_scale":         _FIBONACCI_SCALE,
            "exceeds_single_task_band": overflow,
        }

    for sp, lo, hi, act_lo, act_hi, sessions in _FIBONACCI_STORY_POINTS:
        if mins <= hi:
            return _pack(sp, lo, hi, act_lo, act_hi, sessions, False)
    # Beyond the largest single-task band → still report SP5 but flag as a split candidate.
    sp, lo, hi, act_lo, act_hi, sessions = _FIBONACCI_STORY_POINTS[-1]
    return _pack(sp, lo, hi, act_lo, act_hi, sessions, True)
