"""T8 soak monitor: every 60 s inspect logs/bot.log and the bot process.

Kill rules (any one -> SIGTERM to the bot for a graceful flatten, reason recorded):
  1. a Traceback appears in NEW bot.log content since the monitor started
  2. rss_peak_mb > 300
  3. feed_restarts > 6 in any rolling hour
  4. abs(position notional) > 50% of max_position_notional continuously for > 60 min
     (T8b: replaced "any non-zero position > 30 min"; notional = |pos| x last price
     parsed from bot.log, price unknown -> rule cannot trigger)
  5. actions (places+cancels) in a rolling 60 min window > 600
     (T8d: was actions_min x 60 - an instantaneous rate; one busy minute killed the T8c soak)

If the process is already gone when checked, the monitor records it and exits (never restarts).

    python scripts/soak_monitor.py            # run the monitor
    python scripts/soak_monitor.py --report   # print the human-facing soak summary
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

METRICS_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
FIELD_RE = re.compile(r"(\w+)=(-?[0-9]+(?:\.[0-9]+)?)")
RESTART_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.*feed restart:")
TRACEBACK_MARK = "Traceback (most recent call last)"

# T8b position rule (mirrors RiskConfig.max_position_notional; sync asserted in tests)
MAX_POSITION_NOTIONAL = 80.0
POS_NOTIONAL_FRACTION = 0.5
POS_HOLD_S = 60 * 60.0
ACTIONS_PER_HOUR_LIMIT = 600.0                        # T8d: rolling 60 min window, unchanged limit
ACTIONS_WINDOW = timedelta(minutes=60)
FILL_PX_RE = re.compile(r"\bFILL \S+ -?[0-9.]+ @ (-?[0-9.]+)")
LIMIT_PX_RE = re.compile(r'"limitPx":"(-?[0-9.]+)"')
PX_RE = re.compile(r'"px":"(-?[0-9.]+)"')


def _parse_ts(line: str) -> Optional[datetime]:
    m = METRICS_TS_RE.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


def parse_metrics_line(line: str) -> Optional[dict]:
    """Extract the timestamp + numeric fields of a `metrics book=...` line."""
    if "metrics book=" not in line:
        return None
    fields = {k: float(v) for k, v in FIELD_RE.findall(line)}
    ts = _parse_ts(line)
    return {"ts": ts, **fields} if ts else None


def read_new(path: str, offset: int) -> Tuple[str, int]:
    """Bytes added since offset (offset resets to 0 on rotation/truncation)."""
    if not os.path.exists(path):
        return "", offset
    size = os.path.getsize(path)
    if size < offset:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    return data.decode("utf-8", "replace"), offset + len(data)


def price_from_line(line: str) -> Optional[float]:
    """Last price mentioned on a bot.log line (FILL, our resting limitPx, then any px)."""
    for pat in (FILL_PX_RE, LIMIT_PX_RE, PX_RE):
        m = pat.search(line)
        if m:
            px = float(m.group(1))
            if px > 0:
                return px
    return None


@dataclass
class SoakState:
    restart_times: List[datetime] = field(default_factory=list)
    action_samples: List[Tuple[datetime, float]] = field(default_factory=list)   # (ts, places+cancels)
    last_metrics: Optional[dict] = None
    traceback_seen: bool = False
    price: Optional[float] = None                       # last price seen in bot.log
    last_pos: float = 0.0                               # latest metrics pos
    pos_breach_since: Optional[datetime] = None         # notional above threshold since
    max_position_notional: float = MAX_POSITION_NOTIONAL
    pos_notional_fraction: float = POS_NOTIONAL_FRACTION
    pos_hold_s: float = POS_HOLD_S

    @property
    def pos_notional_threshold(self) -> float:
        return self.pos_notional_fraction * self.max_position_notional

    @property
    def pos_notional(self) -> Optional[float]:
        return abs(self.last_pos) * self.price if self.price is not None else None


def refresh_pos_breach(state: SoakState, ts: datetime) -> None:
    """Start/clear the >threshold-notional hold clock for the rule-4 window."""
    notional = state.pos_notional
    if notional is not None and notional > state.pos_notional_threshold:
        if state.pos_breach_since is None:
            state.pos_breach_since = ts
    else:
        state.pos_breach_since = None


def apply_new_text(state: SoakState, text: str) -> None:
    """Fold new bot.log content into state (traceback, restarts, price, metrics, breach)."""
    if TRACEBACK_MARK in text:
        state.traceback_seen = True
    for line in text.splitlines():
        if RESTART_RE.match(line):
            ts = _parse_ts(line)
            if ts:
                state.restart_times.append(ts)
        px = price_from_line(line)
        if px is not None:
            state.price = px
        m = parse_metrics_line(line)
        if m:
            state.last_metrics = m
            state.last_pos = m.get("pos", 0.0)
            if "places" in m and "cancels" in m:
                _record_action_sample(state, m["ts"], m["places"] + m["cancels"])
        if (px is not None or m) and (ts := _parse_ts(line)) is not None:
            refresh_pos_breach(state, ts)


def _record_action_sample(state: SoakState, ts: datetime, cum_actions: float) -> None:
    """Append a cumulative (places+cancels) sample, keeping the newest sample at/before the 60 min base."""
    samples = state.action_samples
    if samples and ts <= samples[-1][0]:
        return                                          # duplicate or out-of-order metrics line
    samples.append((ts, cum_actions))
    cutoff = ts - ACTIONS_WINDOW
    drop = 0
    while drop + 1 < len(samples) and samples[drop + 1][0] <= cutoff:
        drop += 1                                       # newest sample at/before the cutoff = window base
    if drop:
        del samples[:drop]


def rolling_actions(samples: List[Tuple[datetime, float]], now: datetime) -> Optional[float]:
    """Actions (places+cancels) in the last 60 min. A run/monitor younger than the window counts
    from its start (base 0) so a partial window can only under-count - never kill by itself."""
    if not samples:
        return None
    cutoff = now - ACTIONS_WINDOW
    base = 0.0
    for ts, cum in samples:
        if ts <= cutoff:
            base = cum
        else:
            break
    return samples[-1][1] - base


def evaluate_rules(state: SoakState, now: datetime) -> Optional[str]:
    """Return the first triggered kill reason (fixed precedence) or None."""
    if state.traceback_seen:
        return "Traceback in bot.log"
    m = state.last_metrics
    if m is not None:
        rss = m.get("rss_peak_mb")
        if rss is not None and rss > 300:
            return f"RSS {rss:.0f} MB > 300"
    roll = rolling_actions(state.action_samples, now)
    if roll is not None and roll > ACTIONS_PER_HOUR_LIMIT:
        return f"actions/hour {roll:.0f} (last 60 min) > {ACTIONS_PER_HOUR_LIMIT:.0f}"
    cutoff = now - timedelta(hours=1)
    n = sum(1 for t in state.restart_times if t > cutoff)
    if n > 6:
        return f"feed_restarts {n} > 6 in the last hour"
    if state.pos_breach_since is not None and state.pos_notional is not None:
        held = (now - state.pos_breach_since).total_seconds()
        if held > state.pos_hold_s:
            return (f"position notional ${state.pos_notional:.2f} > "
                    f"{state.pos_notional_fraction:.0%} of max_position_notional "
                    f"(${state.max_position_notional:.0f}) for {held / 60:.0f} min > 60")
    return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- T8c (REQ-0029): host loadavg + bot latency EMA -----------------------------------
def loadavg_line(path: str = "/proc/loadavg") -> str:
    """Contents of /proc/loadavg: 1/5/15-min averages, runnable/total, last spawned pid."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "n/a"


def health_line(state: "SoakState", loadavg: Optional[str] = None) -> str:
    """One greppable line per monitor tick: host load + the bot's latency EMA (last metrics)."""
    la = loadavg if loadavg is not None else loadavg_line()
    m = state.last_metrics or {}
    return (f'HEALTH loadavg="{la}" lat_ema_ms={m.get("lat_ema_ms", 0.0):.0f} '
            f'rss_mb={m.get("rss_peak_mb", 0.0):.0f} pos={m.get("pos", 0.0):.6f} '
            f'fills={int(m.get("fills", 0))} feed_age={m.get("feed_age", 0.0):.2f}')


def log_health(rec: "Recorder", state: "SoakState", loadavg: Optional[str] = None) -> str:
    line = health_line(state, loadavg)
    rec.log(line)
    return line


class Recorder:
    def __init__(self, path: str):
        self.path = path

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        with open(self.path, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)


def run_monitor(pid: int, bot_log: str, interval: float, rec_path: str,
                max_position_notional: float = MAX_POSITION_NOTIONAL) -> int:
    rec = Recorder(rec_path)
    offset = os.path.getsize(bot_log) if os.path.exists(bot_log) else 0
    state = SoakState(max_position_notional=max_position_notional)
    rec.log(f"MONITOR start pid={pid} bot_log={bot_log} interval={interval}s offset={offset} "
            f"pos_rule=notional>{state.pos_notional_threshold:.0f}$ "
            f"({state.pos_notional_fraction:.0%} of max_position_notional="
            f"{max_position_notional:.0f}) for >{state.pos_hold_s / 60:.0f}min")
    try:
        while True:
            time.sleep(interval)
            if not pid_alive(pid):
                rec.log(f"PROC_DEAD pid={pid} (no kill rule fired; process exited on its own)")
                rec.log("MONITOR exit (process died, not restarted)")
                return 2
            text, offset = read_new(bot_log, offset)
            if text:
                apply_new_text(state, text)
            log_health(rec, state)                   # T8c REQ-0029: every tick (= --interval)
            reason = evaluate_rules(state, datetime.now())
            if reason:
                rec.log(f"KILL {reason} -> SIGTERM pid={pid}")
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    rec.log(f"PROC_DEAD pid={pid} died before SIGTERM")
                    return 2
                for _ in range(60):                       # wait up to 120 s for graceful exit
                    if not pid_alive(pid):
                        rec.log(f"PROC_EXITED pid={pid} after SIGTERM ({reason})")
                        rec.log("MONITOR exit (kill rule fired)")
                        return 0
                    time.sleep(2)
                rec.log(f"ALERT pid={pid} still alive 120 s after SIGTERM - needs human")
                return 3
    except KeyboardInterrupt:
        rec.log("MONITOR interrupted by user")
        return 130


# ---- human-facing report -----------------------------------------------------------

def _fmt_last_warnings(bot_log: str, n: int = 5) -> List[str]:
    if not os.path.exists(bot_log):
        return ["(no bot.log yet)"]
    hits = []
    with open(bot_log, errors="replace") as f:
        for line in f:
            if " WARNING " in line or " ERROR " in line:
                hits.append(line.rstrip())
    return hits[-n:] or ["(no WARNING/ERROR lines)"]


def report(bot_log: str, pid: Optional[int]) -> int:
    metrics: List[dict] = []
    kills: List[str] = []
    first_ts = last_ts = None
    price: Optional[float] = None
    if os.path.exists(bot_log):
        with open(bot_log, errors="replace") as f:
            for line in f:
                px = price_from_line(line)
                if px is not None:
                    price = px
                m = parse_metrics_line(line)
                if m:
                    metrics.append(m)
                    if first_ts is None:
                        first_ts = m["ts"]
                    last_ts = m["ts"]
                if "KILL SWITCH" in line:
                    kills.append(line.strip())
    alive = pid is not None and pid_alive(pid)
    uptime = f"{(last_ts - first_ts).total_seconds() / 60:.1f} min (log metrics)" if first_ts else "n/a"
    if alive and pid:
        try:
            with open(f"/proc/{pid}/stat") as f:
                start_ticks = int(f.read().split()[21])
            with open("/proc/stat") as f:
                btime = next(int(l.split()[1]) for l in f if l.startswith("btime "))
            hz = os.sysconf("SC_CLK_TCK")
            uptime = f"{time.time() - (btime + start_ticks / hz):.0f} s"
        except Exception:
            pass
    print(f"SOAK REPORT  now={datetime.now():%Y-%m-%d %H:%M:%S}  "
          f"process={'ALIVE pid=' + str(pid) if alive else 'DEAD'}  uptime={uptime}")
    if metrics:
        last = metrics[-1]
        total_places = int(last.get("places", 0))
        total_cancels = int(last.get("cancels", 0))
        rejects = int(last.get("cross_rejects", 0)) + int(last.get("risk_rejects", 0)) \
            + int(last.get("other_rejects", 0))
        roll = rolling_actions([(m["ts"], m.get("places", 0.0) + m.get("cancels", 0.0))
                                for m in metrics], last["ts"]) or 0.0
        print(f"  fills={int(last.get('fills', 0))} places={total_places} cancels={total_cancels} "
              f"actions/hour={roll:.0f} (rolling 60 min)")
        print(f"  feed_restarts={int(last.get('feed_restarts', 0))} "
              f"stale_pulls={int(last.get('stale_pulls', 0))} rejects={rejects} "
              f"cross={int(last.get('cross_rejects', 0))} risk={int(last.get('risk_rejects', 0))} "
              f"other={int(last.get('other_rejects', 0))} "
              f"place_errors={int(last.get('place_errors', 0))} pos_drift={int(last.get('pos_drift', 0))}")
        notional = f"pos_notional=${abs(last.get('pos', 0.0)) * price:.2f} " if price else ""
        print(f"  max_lat_ema_ms={max(m.get('lat_ema_ms', 0) for m in metrics):.0f} "
              f"max_rss_mb={max(m.get('rss_peak_mb', 0) for m in metrics):.0f} "
              f"equity={last.get('equity', 0):.2f} pos={last.get('pos', 0):.6f} "
              f"{notional}feed_age={last.get('feed_age', 0):.2f} book={int(last.get('book', 0))}")
    else:
        print("  (no metrics lines yet)")
    la = loadavg_line()
    last_lat = metrics[-1].get("lat_ema_ms", 0.0) if metrics else 0.0
    max_lat = max((m.get("lat_ema_ms", 0.0) for m in metrics), default=0.0)
    print(f"  loadavg={la}  lat_ema_ms={last_lat:.0f} (max {max_lat:.0f})")
    print(f"  kills={len(kills)}" + (f" -> {kills[-1][:120]}" if kills else ""))
    print("  last 5 WARNING/ERROR:")
    for line in _fmt_last_warnings(bot_log):
        print(f"    {line[:200]}")
    return 0 if alive or not kills else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="T8 soak monitor")
    ap.add_argument("--report", action="store_true", help="print the human-facing summary")
    ap.add_argument("--pid", type=int, help="bot (python) pid to watch/signal")
    ap.add_argument("--bot-log", default="logs/bot.log")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--monitor-log", default="logs/soak_monitor.log")
    ap.add_argument("--max-position-notional", type=float, default=MAX_POSITION_NOTIONAL,
                    help="USD; rule 4 kills when |pos| x price exceeds 50%% of this for > 60 min "
                         "(default mirrors RiskConfig.max_position_notional)")
    args = ap.parse_args()
    if args.report:
        return report(args.bot_log, args.pid)
    if not args.pid:
        ap.error("--pid is required unless --report")
    return run_monitor(args.pid, args.bot_log, args.interval, args.monitor_log,
                       args.max_position_notional)


if __name__ == "__main__":
    sys.exit(main())
