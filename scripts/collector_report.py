#!/usr/bin/env python3
"""scripts/collector_report.py — REQ-0034 STAGE 3 PHASE B: collector status, max 25 lines.

Offline: reads data/collector_pid.txt, /proc, the current shard directory, logs/collector.log
and disk usage. Never touches the network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw_sample"
PID_FILE = DATA_DIR / "collector_pid.txt"
LOG_FILE = PROJECT_ROOT / "logs" / "collector.log"
ARCHIVE_LOG = PROJECT_ROOT / "logs" / "collector_archive.log"

MAX_LINES = 25
STOP_FREE = 300 * 1024 * 1024
RSS_LIMIT = 100 * 1024 * 1024


def now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_state() -> tuple[int | None, dict]:
    meta: dict = {}
    try:
        lines = PID_FILE.read_text().splitlines()
    except OSError:
        return None, meta
    if not lines:
        return None, meta
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None, meta
    for line in lines[1:]:
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k] = v
    if not Path(f"/proc/{pid}/stat").exists():
        return None, meta
    return pid, meta


def rss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def proc_fields(pid: int) -> list[str]:
    """Tail of /proc/<pid>/stat after ') ' — robust against spaces in the comm."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return []
    _, sep, tail = text.rpartition(") ")
    return tail.split() if sep else []


def uptime_s(pid: int) -> str:
    import os as _os

    fields = proc_fields(pid)
    if len(fields) < 20:
        return "?"
    try:
        btime = next(
            int(ln.split()[1])
            for ln in Path("/proc/stat").read_text().splitlines()
            if ln.startswith("btime ")
        )
        hz = _os.sysconf("SC_CLK_TCK")
        start = btime + int(fields[19]) / hz  # field 22 = starttime
        return f"{int(datetime.now(tz=timezone.utc).timestamp() - start)}s"
    except (OSError, ValueError, StopIteration, IndexError):
        return "?"


def nice_of(pid: int) -> str:
    fields = proc_fields(pid)
    return fields[16] if len(fields) > 16 else "?"  # field 19 = nice


def os_getsid(pid: int) -> int:
    import os

    try:
        return os.getsid(pid)
    except OSError:
        return -1


def tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def last_line(text: str, prefix: str) -> str:
    for line in reversed(text.splitlines()):
        if prefix in line:
            return line
    return "-"


def main() -> int:
    out: list[str] = []
    now = datetime.now(tz=timezone.utc)
    out.append(f"=== collector report {now_utc()} ===")

    pid, meta = pid_state()
    if pid:
        sid = os_getsid(pid)
        out.append(
            f"status   : RUNNING pid={pid} uptime={uptime_s(pid)} "
            f"nice={nice_of(pid)} sid={sid}"
        )
    else:
        out.append(f"status   : NOT RUNNING (pid file: {PID_FILE.relative_to(PROJECT_ROOT)})")

    coins = meta.get("coins", "")
    if not coins:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from collector import load_coins

            coins = ",".join(load_coins(DATA_DIR / "coins.json"))
        except Exception:
            coins = "-"
    n_coins = len([c for c in coins.split(",") if c]) if coins != "-" else 0
    out.append(f"coins    : {n_coins} {coins}")

    if pid:
        used = rss_mb(pid)
        out.append(
            f"rss      : {used:.1f} MB (warn 90, limit 100)" if used is not None
            else "rss      : ? (process gone)"
        )
    else:
        out.append("rss      : -")

    import shutil as _shutil

    free = _shutil.disk_usage(PROJECT_ROOT).free
    out.append(f"disk     : {free / (1 << 30):.2f} GiB free (stop below 300 MB)")

    day = now.strftime("%Y%m%d")
    hh = now.strftime("%H")
    day_dir = DATA_DIR / day
    shard_files = sorted(day_dir.glob("*.jsonl.gz")) if day_dir.is_dir() else []
    hour_files = [p for p in shard_files if p.name.startswith(f"{hh}_")]
    day_bytes = sum(p.stat().st_size for p in shard_files)
    hour_bytes = sum(p.stat().st_size for p in hour_files)
    out.append(
        f"shards   : {day} {len(hour_files)} files {hour_bytes / 1048576:.2f} MB now | "
        f"day {len(shard_files)} files {day_bytes / 1048576:.2f} MB"
    )
    raw_files = sorted((RAW_DIR / day).glob("*.jsonl")) if (RAW_DIR / day).is_dir() else []
    out.append(
        f"raw      : data/raw_sample/{day} {len(raw_files)} files "
        f"{sum(p.stat().st_size for p in raw_files) / 1048576:.2f} MB (first hour only)"
    )

    log = tail(LOG_FILE)
    stat = last_line(log, "] STAT ")
    if stat != "-":
        out.append("stat     : " + stat.split("] ", 1)[-1][:180])
    else:
        out.append("stat     : no STAT line yet (first in 60 s)")
    subs = last_line(log, "subscribed ")
    out.append("subs     : " + (subs.split("] ", 1)[-1][:180] if subs != "-" else "-"))
    out.append("last gap : " + last_line(log, "gap #").split("] ", 1)[-1][:180])
    out.append("last warn: " + last_line(log, "] WARN ").split("] ", 1)[-1][:180])
    out.append("last err : " + last_line(log, "] ERROR").split("] ", 1)[-1][:180])
    arch = last_line(log, "archive run finished")
    out.append("archive  : " + (arch.split("] ", 1)[-1][:180] if arch != "-" else "no run yet"))

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import paper_report

        out.append(paper_report.summary_line(paper_report.load_state(paper_report.STATE_FILE)))
    except Exception as exc:  # never let the paper summary break the collector report
        out.append(f"paper    : unavailable ({type(exc).__name__})")

    log_path = LOG_FILE
    if log_path.exists():
        size = log_path.stat().st_size
        backups = len(list(log_path.parent.glob(log_path.name + ".*")))
        out.append(f"log      : {log_path.relative_to(PROJECT_ROOT)} "
                   f"{size / 1048576:.2f} MB ({backups + 1} files, 5 MB x 3)")
    else:
        out.append("log      : missing")

    out.append(f"log tail : {last_line(log, 'collector').split('] ', 1)[-1][:160]}")

    text = "\n".join(out[:MAX_LINES])
    print(text)
    if len(out) > MAX_LINES:
        print(f"(+{len(out) - MAX_LINES} lines truncated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
