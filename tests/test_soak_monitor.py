"""T8 offline tests for soak_monitor rule logic (no process signalled; pure functions)."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import soak_monitor as sm  # noqa: E402

REAL_METRICS = ("2026-10-05 11:20:22,029 INFO mmbot: metrics book=1530 fills=0 places=18 "
                "cancels=16 cross_rejects=0 risk_rejects=0 other_rejects=0 place_errors=0 "
                "stale_pulls=0 feed_restarts=1 pos_drift=0 orphans_cancelled=0 stale_pruned=0 "
                "actions_min=4 lat_ema_ms=826 pos=0.000000 equity=999.68 rss_peak_mb=63 "
                "feed_age=0.20")

NOW = datetime(2026, 10, 5, 20, 0, 0)


def st(**kw):
    return sm.SoakState(**kw)


def test_parse_metrics_line_real_line():
    m = sm.parse_metrics_line(REAL_METRICS)
    assert m["ts"] == datetime(2026, 10, 5, 11, 20, 22)
    assert m["book"] == 1530 and m["places"] == 18 and m["cancels"] == 16
    assert m["rss_peak_mb"] == 63 and m["pos"] == 0.0 and m["actions_min"] == 4
    assert m["lat_ema_ms"] == 826 and m["feed_age"] == 0.20
    assert sm.parse_metrics_line("2026-10-05 11:16:32,572 WARNING mmbot: feed restart: x") is None


def test_read_new_appends_and_handles_rotation(tmp_path):
    p = str(tmp_path / "bot.log")
    with open(p, "w") as f:
        f.write("line1\n")
    text, off = sm.read_new(p, 0)
    assert text == "line1\n" and off == 6
    with open(p, "a") as f:
        f.write("line2\n")
    text, off = sm.read_new(p, off)
    assert text == "line2\n" and off == 12
    with open(p, "w") as f:                     # rotation: file shrinks -> offset resets
        f.write("fresh\n")
    text, off = sm.read_new(p, off)
    assert text == "fresh\n" and off == 6


def test_apply_new_text_traceback_restart_and_position():
    s = st()
    sm.apply_new_text(s, "2026-10-05 19:00:01 WARNING mmbot: feed restart: a | book_silence=9.0s\n"
                         + REAL_METRICS + "\n")
    assert s.traceback_seen is False
    assert s.restart_times == [datetime(2026, 10, 5, 19, 0, 1)]
    assert s.pos_breach_since is None                         # flat and no price yet
    # price line + metrics with a position above 50% of max_position_notional -> clock starts
    text = ("2026-10-05 11:20:25 INFO mmbot: FILL sell 0.000500 @ 86243.000000 pos=-0.000500\n"
            + REAL_METRICS.replace("pos=0.000000", "pos=-0.000500") + "\n")
    sm.apply_new_text(s, text)
    assert s.price == 86243.0 and s.last_pos == -0.0005
    assert s.pos_breach_since == datetime(2026, 10, 5, 11, 20, 22)   # clock uses the metrics ts
    assert s.pos_notional > s.pos_notional_threshold
    sm.apply_new_text(s, "Boom\nTraceback (most recent call last):\n  File ...\n")
    assert s.traceback_seen is True
    sm.apply_new_text(s, REAL_METRICS)                       # flat again resets the clock
    assert s.pos_breach_since is None


def test_rule_traceback():
    assert sm.evaluate_rules(st(traceback_seen=True), NOW) == "Traceback in bot.log"


def test_rule_rss_boundary():
    base = sm.parse_metrics_line(REAL_METRICS)
    ok = dict(base, rss_peak_mb=300.0)
    bad = dict(base, rss_peak_mb=300.1)
    assert sm.evaluate_rules(st(last_metrics=ok), NOW) is None
    assert sm.evaluate_rules(st(last_metrics=bad), NOW) == "RSS 300 MB > 300"


def test_rule_actions_per_hour_boundary():
    """Rule 5 = actions (places+cancels) in a rolling 60 min window; limit 600/hour."""
    at = st(action_samples=[(NOW - timedelta(minutes=30), 0.0), (NOW, 600.0)])
    over = st(action_samples=[(NOW - timedelta(minutes=30), 0.0), (NOW, 601.0)])
    assert sm.evaluate_rules(at, NOW) is None                              # 600 not > 600
    r = sm.evaluate_rules(over, NOW)
    assert r and "actions/hour 601" in r and "> 600" in r


def test_rule_actions_61_min_old_spike_no_longer_counts():
    """A burst that fell out of the window must not kill (the T8c soak died on an instant rate)."""
    old = [(NOW - timedelta(minutes=70), 0.0), (NOW - timedelta(minutes=61), 700.0), (NOW, 760.0)]
    assert sm.evaluate_rules(st(action_samples=old), NOW) is None          # 760 - 700 = 60 in window
    young = [(NOW - timedelta(minutes=70), 0.0), (NOW - timedelta(minutes=59), 700.0), (NOW, 760.0)]
    r = sm.evaluate_rules(st(action_samples=young), NOW)
    assert r and "actions/hour 760" in r                                   # still inside the window


def test_rule_actions_partial_window_counts_from_run_start():
    """A run younger than 60 min counts everything it did (base = 0, never a false negative)."""
    assert sm.evaluate_rules(st(action_samples=[(NOW - timedelta(minutes=20), 0.0), (NOW, 600.0)]),
                             NOW) is None
    r = sm.evaluate_rules(st(action_samples=[(NOW - timedelta(minutes=20), 0.0), (NOW, 601.0)]), NOW)
    assert r and "actions/hour 601" in r
    assert sm.evaluate_rules(st(action_samples=[]), NOW) is None           # no metrics yet


def test_apply_new_text_records_action_samples():
    s = st()
    sm.apply_new_text(s, REAL_METRICS)
    m = sm.parse_metrics_line(REAL_METRICS)
    assert s.action_samples == [(m["ts"], m["places"] + m["cancels"])]
    later = REAL_METRICS.replace("places=18", "places=30").replace("cancels=16", "cancels=22") \
                         .replace("2026-10-05 11:20:22", "2026-10-05 11:21:22")
    sm.apply_new_text(s, later)
    assert len(s.action_samples) == 2 and s.action_samples[-1][1] == 52
    # window ending 60 min after the 1st sample: only the 2nd sample is inside it -> 52 - 34
    assert sm.rolling_actions(s.action_samples, m["ts"] + timedelta(minutes=60)) == 18
    # one minute later the 1st sample has left the window entirely -> nothing left to count
    assert sm.rolling_actions(s.action_samples, m["ts"] + timedelta(minutes=61)) == 0
    assert sm.rolling_actions([], NOW) is None


def test_rule_feed_restarts_rolling_hour():
    seven = [NOW - timedelta(minutes=59 - i) for i in range(7)]
    six = [NOW - timedelta(minutes=59 - i) for i in range(6)]
    assert sm.evaluate_rules(st(restart_times=six), NOW) is None
    r = sm.evaluate_rules(st(restart_times=seven), NOW)
    assert r == "feed_restarts 7 > 6 in the last hour"
    old = [NOW - timedelta(hours=2)] * 8                     # outside the window
    assert sm.evaluate_rules(st(restart_times=old), NOW) is None


def test_rule_pos_notional_threshold_boundary():
    """Both sides of 50% of max_position_notional (0.5 x 80 = $40), price $80/unit."""
    below = st(price=80.0, last_pos=0.49)                    # $39.20 < $40
    at = st(price=80.0, last_pos=0.50)                       # exactly $40 -> strict >
    above = st(price=80.0, last_pos=0.51)                    # $40.80 > $40
    for s in (below, at):
        sm.refresh_pos_breach(s, NOW - timedelta(minutes=120))
        assert s.pos_breach_since is None
    sm.refresh_pos_breach(above, NOW - timedelta(minutes=120))
    assert above.pos_breach_since == NOW - timedelta(minutes=120)
    # clock resets as soon as notional falls back to/below the threshold
    above.last_pos = 0.40
    sm.refresh_pos_breach(above, NOW)
    assert above.pos_breach_since is None


def test_rule_pos_notional_hold_time_boundary():
    """Both sides of the >60 min hold."""
    at_60 = st(price=80.0, last_pos=0.51,
               pos_breach_since=NOW - timedelta(minutes=60))
    past_60 = st(price=80.0, last_pos=0.51,
                 pos_breach_since=NOW - timedelta(minutes=60, seconds=1))
    just_under = st(price=80.0, last_pos=0.51,
                    pos_breach_since=NOW - timedelta(minutes=59, seconds=59))
    assert sm.evaluate_rules(at_60, NOW) is None              # exactly 60 min is not > 60
    assert sm.evaluate_rules(just_under, NOW) is None
    r = sm.evaluate_rules(past_60, NOW)
    assert r and r.startswith("position notional $40.80 > 50% of max_position_notional ($80)")

    # no price -> rule cannot evaluate -> never triggers (even with a large position)
    no_price = st(last_pos=-1.0, pos_breach_since=NOW - timedelta(hours=12))
    assert sm.evaluate_rules(no_price, NOW) is None


def test_rule_pos_notional_t8_kill_case_no_longer_triggers():
    """The T8a case that killed the first soak: tiny inventory held a long time."""
    t8 = st(price=86243.0, last_pos=-0.000280,               # $24.15 = 30% of $80
            pos_breach_since=NOW - timedelta(minutes=90))
    sm.refresh_pos_breach(t8, NOW)                           # still below threshold
    assert t8.pos_breach_since is None
    assert sm.evaluate_rules(t8, NOW) is None

    big = st(price=86243.0, last_pos=-0.000600)              # $51.75 > $40
    sm.refresh_pos_breach(big, NOW - timedelta(minutes=61))
    r = sm.evaluate_rules(big, NOW)
    assert r and "position notional $51.75" in r and "min > 60" in r


def test_max_position_notional_matches_risk_config():
    from mmbot.models import RiskConfig
    assert sm.MAX_POSITION_NOTIONAL == RiskConfig().max_position_notional


def test_price_from_line_patterns():
    assert sm.price_from_line(
        "2026-10-05 12:43:43,477 INFO mmbot: FILL sell 0.000010 @ 86073.000000 pos=0.000010"
    ) == 86073.0
    assert sm.price_from_line(
        '2026-10-05 12:43:44,156 INFO mmbot.gateway: orderUpdates raw: '
        '{"order":{"coin":"BTC","side":"B","limitPx":"86064.0","sz":"0.00017"}}'
    ) == 86064.0
    assert sm.price_from_line(REAL_METRICS) is None


def test_no_rules_no_trigger():
    assert sm.evaluate_rules(st(), NOW) is None
    assert sm.evaluate_rules(st(last_metrics=sm.parse_metrics_line(REAL_METRICS)), NOW) is None


def test_rule_precedence_traceback_first():
    s = st(traceback_seen=True,
           last_metrics=sm.parse_metrics_line(REAL_METRICS).update(
               rss_peak_mb=999, actions_min=99) or sm.parse_metrics_line(REAL_METRICS))
    # precedence: Traceback wins even with other breaches present
    s.last_metrics = dict(s.last_metrics, rss_peak_mb=999.0, actions_min=99.0)
    assert sm.evaluate_rules(s, NOW) == "Traceback in bot.log"


# ---- T8c (REQ-0029): host loadavg + bot latency EMA observability ----------------------
LA_RE = __import__("re").compile(r"^\d+\.\d+ \d+\.\d+ \d+\.\d+ \d+/\d+ \d+$")


def test_loadavg_line_reads_proc_loadavg():
    la = sm.loadavg_line()
    assert LA_RE.match(la), f"unexpected /proc/loadavg content: {la!r}"


def test_health_line_includes_loadavg_and_lat_ema():
    state = st()
    sm.apply_new_text(state, REAL_METRICS)
    line = sm.health_line(state, loadavg="0.52 0.44 0.39 1/234 5678")
    assert 'loadavg="0.52 0.44 0.39 1/234 5678"' in line
    assert "lat_ema_ms=826" in line
    assert line.startswith("HEALTH ")
    assert "rss_mb=63" in line and "pos=0.000000" in line


def test_health_line_without_metrics_still_reports_loadavg():
    line = sm.health_line(st(), loadavg="1.00 1.00 1.00 1/1 1")
    assert 'loadavg="1.00 1.00 1.00 1/1 1"' in line and "lat_ema_ms=0" in line


def test_log_health_appends_to_monitor_log(tmp_path):
    path = tmp_path / "monitor.log"
    rec = sm.Recorder(str(path))
    state = st()
    sm.apply_new_text(state, REAL_METRICS)
    sm.log_health(rec, state, loadavg="0.52 0.44 0.39 1/234 5678")
    out = path.read_text()
    assert "HEALTH" in out and "loadavg=" in out and "lat_ema_ms=826" in out
    assert out.splitlines()[0].count(" ") >= 2                    # timestamped like every MONITOR line


def test_report_prints_loadavg_and_lat_ema(tmp_path, capsys):
    bot_log = tmp_path / "bot.log"
    bot_log.write_text(REAL_METRICS + "\n")
    sm.report(str(bot_log), None)
    out = capsys.readouterr().out
    assert "loadavg=" in out, out
    assert "lat_ema_ms=826" in out, out
