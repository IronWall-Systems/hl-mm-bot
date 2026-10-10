#!/usr/bin/env python3
"""scripts/collector.py — REQ-0034 STAGE 3 PHASE B: public market-data collector.

Read-only: one public WebSocket connection (no keys, no wallet, no signing, no orders) that
subscribes, for each of the 10 approved coins in data/coins.json, to
`l2Book {"fast": true}`, `trades` and `bbo`.

Output
    data/YYYYMMDD/HH_<coin>.jsonl.gz   gzip JSONL, one shard per UTC hour per coin
    data/YYYYMMDD/HH_gaps.jsonl.gz     one line per reconnect ("gap" record)
    data/raw_sample/YYYYMMDD/HH_<coin>.jsonl   the FIRST hour of the run, raw JSON, plain file
Three compact schemas (one line each):
    l2  {"t":ms,"c":coin,"e":"l2","b":[[px,sz,n],..],"a":[[px,sz,n],..]}
    bbo {"t":ms,"c":coin,"e":"bbo","b":[px,sz,n],"a":[px,sz,n]}
    tr  {"t":ms,"c":coin,"e":"tr","side":"B|A","px":px,"sz":sz,"tid":tid}

Runtime guarantees
    * one connection, exponential reconnect backoff (1 s -> 60 s), a malformed message can
      never kill the process, SIGTERM/SIGINT shut it down cleanly;
    * `nice -n 10` at launch; own log logs/collector.log rotated at 5 MB x 3 files;
    * every 5 minutes, and only while free < 1 GiB, it spawns
      `archive_data.py --auto --apply` (that script refuses to run unless it needs to);
    * it stops cleanly (no deletion of its own) when free space < 300 MB;
    * PID written to data/collector_pid.txt.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw_sample"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_LOG = LOG_DIR / "collector.log"
DEFAULT_PID_FILE = DATA_DIR / "collector_pid.txt"
DEFAULT_COINS_JSON = DATA_DIR / "coins.json"
ARCHIVE_SCRIPT = PROJECT_ROOT / "scripts" / "archive_data.py"

WS_URL = "wss://api.hyperliquid.xyz/ws"
SUBSCRIPTIONS_PER_COIN = (
    {"type": "l2Book", "fast": True},
    {"type": "trades"},
    {"type": "bbo"},
)

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_FILES = 3
ARCHIVE_EVERY_S = 300
AUTO_RUN_FREE = 1 << 30
STOP_FREE_BYTES = 300 * 1024 * 1024
RSS_LIMIT_BYTES = 100 * 1024 * 1024
RSS_WARN_BYTES = 90 * 1024 * 1024
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
STABLE_CONNECTION_S = 10.0
PING_EVERY_S = 30.0
RECV_TIMEOUT_S = 1.0
STAT_EVERY_S = 60.0
HOUSEKEEP_EVERY_S = 1.0


class CollectorError(RuntimeError):
    """Fatal collector condition (always handled — the loop never dies on it)."""


def now_utc(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_key(ts: float) -> tuple[str, str]:
    """(YYYYMMDD, HH) for a unix timestamp, in UTC."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y%m%d"), dt.strftime("%H")


def load_coins(path: Path) -> list[str]:
    """The Phase A selection — 10 coins, in file order."""
    try:
        doc = json.loads(Path(path).read_text())
    except FileNotFoundError as exc:
        raise CollectorError(f"{path} is missing — run Phase A first") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError(f"{path} is not valid json: {exc}") from exc
    raw = doc.get("coins") if isinstance(doc, dict) else None
    if not isinstance(raw, list) or not raw:
        raise CollectorError(f"{path} has no 'coins' selection")
    coins = [str(c.get("coin")) for c in raw if isinstance(c, dict) and c.get("coin")]
    if not coins:
        raise CollectorError(f"{path} has no usable coin names")
    return coins


def rss_bytes(status_path: str = "/proc/self/status") -> int:
    """VmRSS in bytes; 0 when /proc is unavailable."""
    try:
        for line in Path(status_path).read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


# --------------------------------------------------------------------------- compact schemas
def compact_l2(data: dict) -> dict | None:
    levels = data.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        return None
    bids, asks = levels
    if not isinstance(bids, list) or not isinstance(asks, list):
        return None

    def pack(levels_) -> list:
        out = []
        for lvl in levels_[:50]:
            if isinstance(lvl, dict):
                out.append([lvl.get("px"), lvl.get("sz"), lvl.get("n")])
        return out

    return {
        "t": data.get("time"),
        "c": data.get("coin"),
        "e": "l2",
        "b": pack(bids),
        "a": pack(asks),
    }


def compact_bbo(data: dict) -> dict | None:
    top = data.get("bbo")
    if not isinstance(top, list) or len(top) != 2:
        return None

    def side(lvl):
        if isinstance(lvl, dict):
            return [lvl.get("px"), lvl.get("sz"), lvl.get("n")]
        return None

    return {
        "t": data.get("time"),
        "c": data.get("coin"),
        "e": "bbo",
        "b": side(top[0]),
        "a": side(top[1]),
    }


def compact_trade(item: dict) -> dict | None:
    if not isinstance(item, dict) or not item.get("coin"):
        return None
    return {
        "t": item.get("time"),
        "c": item.get("coin"),
        "e": "tr",
        "side": item.get("side"),
        "px": item.get("px"),
        "sz": item.get("sz"),
        "tid": item.get("tid"),
    }


def dispatch(message: str, counts: Counter) -> list[tuple[str, dict]]:
    """One raw frame -> [(coin, compact record)]; pong/subscriptions/other -> [].

    A malformed frame is counted and ignored — it never raises.
    """
    try:
        obj = json.loads(message)
    except (ValueError, TypeError):
        counts["malformed"] += 1
        return []
    channel = obj.get("channel")
    data = obj.get("data")
    if channel == "pong" or channel == "subscriptionResponse":
        counts["control"] += 1
        return []
    try:
        if channel == "l2Book" and isinstance(data, dict):
            rec = compact_l2(data)
            if rec and rec.get("c"):
                counts["l2"] += 1
                return [(rec["c"], rec)]
        elif channel == "bbo" and isinstance(data, dict):
            rec = compact_bbo(data)
            if rec and rec.get("c"):
                counts["bbo"] += 1
                return [(rec["c"], rec)]
        elif channel == "trades" and isinstance(data, list):
            out = []
            for item in data:
                rec = compact_trade(item)
                if rec:
                    counts["tr"] += 1
                    out.append((rec["c"], rec))
            return out
        counts["other"] += 1
    except Exception:
        counts["malformed"] += 1
    return []


# --------------------------------------------------------------------------- log + shards
class RotatingLog:
    """Append-only text log rotated at `max_bytes`, keeping `files` files in total."""

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, files: int = LOG_FILES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.files = files
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, level: str, msg: str) -> None:
        line = f"[{now_utc()}] {level:<5} {msg}\n"
        try:
            if self._fh.tell() + len(line) > self.max_bytes:
                self._rotate()
            self._fh.write(line)
            self._fh.flush()
        except ValueError:
            self._fh = self.path.open("a", encoding="utf-8")
            self._fh.write(line)
            self._fh.flush()
        except OSError:
            pass

    def _rotate(self) -> None:
        self._fh.close()
        keep = max(self.files - 1, 1)
        oldest = self.path.with_name(f"{self.path.name}.{keep}")
        if oldest.exists():
            oldest.unlink()
        for i in range(keep - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._fh = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


class HourShards:
    """gzip JSONL shards, one per UTC hour per coin, plus the raw first hour."""

    def __init__(
        self,
        data_dir: Path,
        raw_dir: Path,
        coins: list[str],
        *,
        now=time.time,
        raw_hours: int = 1,
        compresslevel: int = 6,
    ):
        self.data_dir = Path(data_dir)
        self.raw_dir = Path(raw_dir)
        self.coins = list(coins)
        self.now = now
        self.raw_hours = raw_hours
        self.compresslevel = compresslevel
        self.hour_index = -1
        self.date = ""
        self.hour = ""
        self.handles: dict[tuple[str, str], object] = {}
        self.raw_handles: dict[tuple[str, str], object] = {}
        self.lines: Counter = Counter()
        self.bytes_out = 0
        self.files: set[str] = set()
        self.errors = 0
        self._roll(self.now())

    def _paths(self, date: str, hh: str, coin: str) -> tuple[Path, Path]:
        day_dir = self.data_dir / date
        return day_dir / f"{hh}_{coin}.jsonl.gz", self.raw_dir / date / f"{hh}_{coin}.jsonl"

    def _roll(self, ts: float) -> None:
        date, hh = hour_key(ts)
        if date == self.date and hh == self.hour and self.hour_index >= 0:
            return
        self._close_all()
        self.date, self.hour = date, hh
        self.hour_index += 1
        (self.data_dir / date).mkdir(parents=True, exist_ok=True)

    def _close_all(self) -> None:
        for fh in list(self.handles.values()) + list(self.raw_handles.values()):
            try:
                fh.close()
            except OSError:
                pass
        self.handles.clear()
        self.raw_handles.clear()

    def _gz(self, date: str, hh: str, coin: str):
        key = (date, hh, coin)
        fh = self.handles.get(key)
        if fh is None:
            path, _ = self._paths(date, hh, coin)
            fh = gzip.open(path, "at", encoding="utf-8", compresslevel=self.compresslevel)
            self.handles[key] = fh
            self.files.add(str(path))
        return fh

    def _raw(self, date: str, hh: str, coin: str):
        key = (date, hh, coin)
        fh = self.raw_handles.get(key)
        if fh is None:
            _, path = self._paths(date, hh, coin)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = path.open("at", encoding="utf-8")
            self.raw_handles[key] = fh
            self.files.add(str(path))
        return fh

    def write(self, coin: str, record: dict) -> None:
        self._roll(self.now())
        line = json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n"
        try:
            fh = self._gz(self.date, self.hour, coin)
            fh.write(line)
            if self.hour_index < self.raw_hours:
                self._raw(self.date, self.hour, coin).write(line)
            self.lines[coin] += 1
            self.bytes_out += len(line)
        except OSError:
            self.errors += 1
            raise

    def write_gap(self, record: dict) -> None:
        self._roll(self.now())
        line = json.dumps(record, separators=(",", ":")) + "\n"
        try:
            key = (self.date, self.hour, "_gaps")
            fh = self.handles.get(key)
            if fh is None:
                path = self.data_dir / self.date / f"{self.hour}_gaps.jsonl.gz"
                fh = gzip.open(path, "at", encoding="utf-8", compresslevel=self.compresslevel)
                self.handles[key] = fh
                self.files.add(str(path))
            fh.write(line)
            self.lines["_gaps"] += 1
        except OSError:
            self.errors += 1

    def close(self) -> None:
        self._close_all()


# --------------------------------------------------------------------------- connection
class WsConnection:
    """Synchronous public-websocket wrapper (recv timeout = housekeeping tick)."""

    def __init__(self, url: str = WS_URL, recv_timeout: float = RECV_TIMEOUT_S):
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise CollectorError("refusing to open a real websocket inside a test (offline)")
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - environment guard
            raise CollectorError("websocket-client is not installed") from exc
        self._ws = websocket.create_connection(url, timeout=recv_timeout, http_proxy_host=None)
        self._recv_timeout = recv_timeout

    def send(self, payload: str) -> None:
        self._ws.send(payload)

    def recv(self) -> str:
        import websocket

        try:
            return self._ws.recv()
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError("recv timeout") from exc
        except websocket.WebSocketConnectionClosedException as exc:
            raise ConnectionError("connection closed") from exc

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- collector
class Collector:
    def __init__(
        self,
        coins: list[str],
        *,
        connect=None,
        shards: HourShards | None = None,
        log: RotatingLog | None = None,
        now=time.time,
        sleep=time.sleep,
        disk_usage=shutil.disk_usage,
        spawn_archive=None,
        rss=rss_bytes,
        data_dir: Path = DATA_DIR,
        archive_enabled: bool = True,
        recv_timeout: float = RECV_TIMEOUT_S,
    ):
        self.coins = list(coins)
        self.connect = connect or (lambda: WsConnection(recv_timeout=recv_timeout))
        self.log = log or RotatingLog(DEFAULT_LOG)
        self.data_dir = Path(data_dir)
        self.shards = shards or HourShards(self.data_dir, self.data_dir / "raw_sample", coins)
        self.now = now
        self.sleep = sleep
        self.disk_usage = disk_usage
        self.rss = rss
        self.archive_enabled = archive_enabled
        self.spawn_archive = spawn_archive or self._spawn_archive
        self.counts: Counter = Counter()
        self.stop_reason: str | None = None
        self.reconnects = 0
        self.archive_proc: subprocess.Popen | None = None
        self.archive_log_path = LOG_DIR / "collector_archive.log"
        self.archive_log_fh = None
        self.archive_runs = 0
        self.archive_last_rc: int | None = None
        self.archive_last_output = ""
        self.subscribed = 0
        self.sub_errors = 0
        self.messages = 0
        self.ws = None
        self._last_housekeep = 0.0
        self._last_rss = 0.0
        self._last_stat = 0.0
        self._last_archive = 0.0
        self._last_ping = 0.0
        self._connected_at: float | None = None

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self.log.write("INFO", f"stop requested: {reason}")

    def _spawn_archive(self, cmd: list[str]) -> object:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if self.archive_log_fh:
            try:
                self.archive_log_fh.close()
            except OSError:
                pass
        self.archive_log_fh = self.archive_log_path.open("w", encoding="utf-8")
        return subprocess.Popen(
            cmd,
            stdout=self.archive_log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )

    def housekeeping(self, *, force: bool = False) -> None:
        now = self.now()
        if not force and (now - self._last_housekeep) < HOUSEKEEP_EVERY_S:
            return
        self._last_housekeep = now

        free = int(self.disk_usage(self.data_dir if self.data_dir.exists() else PROJECT_ROOT).free)
        if free < STOP_FREE_BYTES:
            self.request_stop(
                f"free space {free} bytes < 300 MB — stopping cleanly, nothing deleted"
            )
            return

        if (now - self._last_rss) >= 60.0:
            self._last_rss = now
            used = int(self.rss())
            if used >= RSS_LIMIT_BYTES:
                self.log.write("WARN", f"rss {used / 1048576:.1f} MB >= 100 MB limit")
            elif used >= RSS_WARN_BYTES:
                self.log.write("WARN", f"rss {used / 1048576:.1f} MB >= 90 MB (limit 100 MB)")

        if self.archive_proc is not None:
            rc = self.archive_proc.poll()
            if rc is not None:
                self.archive_last_rc = rc
                self.archive_last_output = self._read_archive_log()
                self.log.write(
                    "INFO",
                    f"archive run finished rc={rc} "
                    f"({self.archive_last_output.strip()[-400:] or 'no output'})",
                )
                if self.archive_log_fh:
                    try:
                        self.archive_log_fh.close()
                    except OSError:
                        pass
                    self.archive_log_fh = None
                self.archive_proc = None
                self._last_archive = now
                if int(self.disk_usage(PROJECT_ROOT).free) < STOP_FREE_BYTES:
                    self.request_stop("free space < 300 MB after archiving — stopping cleanly")
        elif (
            self.archive_enabled
            and free < AUTO_RUN_FREE
            and (now - self._last_archive) >= ARCHIVE_EVERY_S
        ):
            cmd = [sys.executable, str(ARCHIVE_SCRIPT), "--auto", "--apply"]
            try:
                self.archive_proc = self.spawn_archive(cmd)
                self.archive_runs += 1
                self._last_archive = now
                self.log.write("INFO", "spawned: " + " ".join(cmd[1:]))
            except OSError as exc:
                self.log.write("ERROR", f"could not spawn archive: {exc}")

        if (now - self._last_stat) >= STAT_EVERY_S:
            self._last_stat = now
            self._stat(free=free)

    def _read_archive_log(self) -> str:
        try:
            return self.archive_log_path.read_text()[-2000:]
        except OSError:
            return ""

    def _stat(self, *, free: int) -> None:
        used = int(self.rss())
        self.log.write(
            "STAT",
            f"hour={self.shards.date}/{self.shards.hour} lines={sum(self.shards.lines.values())} "
            f"l2={self.counts['l2']} bbo={self.counts['bbo']} tr={self.counts['tr']} "
            f"malformed={self.counts['malformed']} reconnects={self.reconnects} "
            f"rss_mb={used / 1048576:.1f} free_mb={free / 1048576:.0f} "
            f"msgs_in={self.messages} subs={self.subscribed}",
        )

    def _subscribe(self, ws) -> None:
        for coin in self.coins:
            for sub in SUBSCRIPTIONS_PER_COIN:
                payload = dict(sub)
                payload["coin"] = coin
                try:
                    ws.send(json.dumps({"method": "subscribe", "subscription": payload},
                                       separators=(",", ":")))
                    self.subscribed += 1
                    self.counts["subs_sent"] += 1
                except Exception as exc:
                    self.sub_errors += 1
                    self.log.write("ERROR", f"subscribe failed {payload}: {exc}")
                self.sleep(0.01)
        self.log.write("INFO", f"subscribed {self.subscribed} "
                               f"({len(self.coins)} coins x {len(SUBSCRIPTIONS_PER_COIN)} feeds)")

    def _handle(self, message: str) -> None:
        self.messages += 1
        for coin, record in dispatch(message, self.counts):
            try:
                self.shards.write(coin, record)
            except OSError as exc:
                self.log.write("ERROR", f"shard write failed: {exc}")
                self.request_stop(f"shard write failed: {exc}")
                return

    def _gap(self, reason: str, since: float | None) -> None:
        self.reconnects += 1
        rec = {
            "t": int(self.now() * 1000),
            "e": "gap",
            "reason": reason,
            "since_ms": int((since or self.now()) * 1000),
            "reconnect": self.reconnects,
        }
        self.shards.write_gap(rec)
        self.log.write("WARN", f"gap #{self.reconnects}: {reason}")

    def read_loop(self, ws, *, max_loops: int | None = None) -> None:
        loops = 0
        while self.stop_reason is None:
            loops += 1
            if max_loops is not None and loops > max_loops:
                self.request_stop(f"max_loops {max_loops} reached")
                break
            try:
                message = ws.recv()
            except TimeoutError:
                self.housekeeping()
                continue
            except Exception as exc:
                raise ConnectionError(str(exc)) from exc
            if message is None:
                self.housekeeping()
                continue
            self._handle(message)
            now = self.now()
            if (now - self._last_ping) >= PING_EVERY_S:
                self._last_ping = now
                try:
                    ws.send(json.dumps({"method": "ping"}))
                except Exception as exc:
                    self.log.write("WARN", f"ping failed: {exc}")
            if (now - self._last_housekeep) >= HOUSEKEEP_EVERY_S:
                self.housekeeping()

    def run(self, *, max_loops: int | None = None) -> int:
        self.log.write(
            "INFO",
            f"collector starting: {len(self.coins)} coins "
            f"({', '.join(self.coins)}) — l2Book fast + trades + bbo, one connection",
        )
        backoff = BACKOFF_START_S
        while self.stop_reason is None:
            self.housekeeping(force=True)
            if self.stop_reason:
                break
            ws = None
            connected_at = self.now()
            try:
                ws = self.connect()
                self.ws = ws
                self._connected_at = connected_at
                self._subscribe(ws)
                self._last_ping = connected_at
                self.read_loop(ws, max_loops=max_loops)
            except Exception as exc:
                if self.stop_reason is None:
                    if (self.now() - connected_at) >= STABLE_CONNECTION_S:
                        backoff = BACKOFF_START_S
                    self._gap(f"{type(exc).__name__}: {exc}", connected_at)
                    self.log.write("WARN", f"reconnect in {backoff:.0f}s")
                    try:
                        self.sleep(backoff)
                    except Exception:
                        pass
                    backoff = min(backoff * 2, BACKOFF_MAX_S)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self.ws = None
            if self.stop_reason:
                break
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self.archive_proc is not None and self.archive_proc.poll() is None:
            self.log.write("INFO", "waiting for the running archive job to finish")
            try:
                self.archive_proc.wait(timeout=120)
                self.archive_last_rc = self.archive_proc.returncode
            except Exception:
                self.log.write("WARN", "archive job still running at shutdown")
        if self.archive_log_fh:
            try:
                self.archive_log_fh.close()
            except OSError:
                pass
        try:
            self.shards.close()
        except Exception:
            pass
        self.log.write(
            "INFO",
            f"collector stopped: {self.stop_reason or 'no reason'} | "
            f"lines={sum(self.shards.lines.values())} l2={self.counts['l2']} "
            f"bbo={self.counts['bbo']} tr={self.counts['tr']} reconnects={self.reconnects}",
        )
        self.log.close()


def write_pid_file(path: Path, coins: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"{os.getpid()}\n"
        f"started_utc={now_utc()}\n"
        f"coins={','.join(coins)}\n"
        f"ws={WS_URL}\n"
        f"log={DEFAULT_LOG.relative_to(PROJECT_ROOT)}\n"
        f"feeds=l2Book(fast)+trades+bbo, one connection\n"
    )
    path.write_text(body)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="collector.py",
        description="Public market-data collector for the 10 approved coins (Phase B).",
    )
    ap.add_argument("--coins-json", type=Path, default=DEFAULT_COINS_JSON)
    ap.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
    ap.add_argument("--log-file", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--no-archive", action="store_true",
                    help="never spawn archive_data.py (manual debugging only)")
    args = ap.parse_args(list(sys.argv[1:]) if argv is None else list(argv))

    try:
        coins = load_coins(args.coins_json)
    except CollectorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    log = RotatingLog(args.log_file)
    shards = HourShards(DATA_DIR, RAW_DIR, coins)
    collector = Collector(
        coins,
        shards=shards,
        log=log,
        archive_enabled=not args.no_archive,
    )

    def _on_signal(signum, _frame):
        collector.request_stop(f"signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            pass

    write_pid_file(args.pid_file, coins)
    try:
        return collector.run()
    except KeyboardInterrupt:
        collector.request_stop("keyboard interrupt")
        collector.shutdown()
        return 0
    except Exception as exc:  # never crash silently
        log.write("ERROR", f"fatal: {type(exc).__name__}: {exc}")
        try:
            collector.shutdown()
        finally:
            return 1


if __name__ == "__main__":
    sys.exit(main())
