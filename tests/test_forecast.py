"""When a limit runs out at the recent pace (forecast_runout), and that build_state sends it to the page."""
import datetime as dt

from ai_meter import cli

NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.timezone.utc)


def win(pct, reset_in_h=3, size_h=5):
    return {"key": "five_hour", "used_pct": pct, "resets_at": cli.iso(NOW + dt.timedelta(hours=reset_in_h)),
            "window_seconds": size_h * 3600}


def pts(*pairs):
    """(minutes ago, percent) pairs to readings."""
    return [(cli.iso(NOW - dt.timedelta(minutes=m)), p) for m, p in pairs]


def test_runs_out_before_the_reset():
    # 20% an hour from 60%: 100% in 2 hours, reset in 3
    f = cli.forecast_runout(win(60), pts((60, 40), (30, 50), (0, 60)), NOW)
    assert f["runout"] == cli.iso(NOW + dt.timedelta(hours=2))
    assert f["pace_pct_per_hour"] == 20.0


def test_lasts_until_the_reset():
    f = cli.forecast_runout(win(60, reset_in_h=1), pts((60, 50), (0, 60)), NOW)
    assert f == {"runout": None, "pace_pct_per_hour": 10.0}


def test_flat_or_falling_use_never_runs_out():
    assert cli.forecast_runout(win(60), pts((60, 60), (0, 60)), NOW) == {"runout": None, "pace_pct_per_hour": 0.0}


def test_already_over_the_limit():
    assert cli.forecast_runout(win(100), [], NOW) == {"runout": "now"}


def test_too_little_to_go_on():
    assert cli.forecast_runout(win(60), pts((0, 60)), NOW) == {}            # one reading
    assert cli.forecast_runout(win(60), pts((5, 55), (0, 60)), NOW) == {}   # under 10 minutes apart
    assert cli.forecast_runout({**win(60), "resets_at": None}, pts((60, 40), (0, 60)), NOW) == {}
    assert cli.forecast_runout({**win(60), "window_seconds": 60}, pts((60, 40), (0, 60)), NOW) == {}  # per-minute limit
    assert cli.forecast_runout({**win(None)}, pts((60, 40), (0, 60)), NOW) == {}


def test_readings_from_before_this_window_are_ignored():
    # The window started 2 hours ago (5 h window, reset in 3 h); the 90% reading belongs to the last one.
    f = cli.forecast_runout(win(30), pts((150, 90), (60, 10), (0, 30)), NOW)
    assert f["pace_pct_per_hour"] == 20.0


def test_only_the_last_six_hours_count_for_long_windows():
    # Weekly window: slow for days, fast in the last hour. The recent pace is what matters.
    w = win(70, reset_in_h=72, size_h=168)
    f = cli.forecast_runout(w, pts((60 * 48, 10), (60, 60), (0, 70)), NOW)
    assert f["pace_pct_per_hour"] == 10.0 and f["runout"] == cli.iso(NOW + dt.timedelta(hours=3))


def test_bad_timestamps_do_not_break_the_state():
    assert cli.forecast_runout({**win(60), "resets_at": "not a date"}, pts((60, 40), (0, 60)), NOW) == {}
