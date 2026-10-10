#!/usr/bin/env python3
"""scripts/archive_data.py — T9c: archive closed data/YYYYMMDD days to the backup mount.

DRY-RUN BY DEFAULT. Nothing is written, renamed or deleted unless `--apply` is given.

For every eligible day (a `data/YYYYMMDD` directory strictly older than the current UTC day,
oldest first, newest 2 closed days always kept locally):

    1. mount safety  — refuses (clear error) unless ALL of:
         * /root/gdrive_backup exists,
         * its st_dev differs from the st_dev of data/ (i.e. it is NOT the local disk),
         * a 1 MiB write + read-back probe file succeeds and is removed again.
    2. tar           — data_YYYYMMDD.tar (no compression) built as `.../hl-datasets/
                       data_YYYYMMDD.tar.partial`, with manifest `YYYYMMDD.manifest.json`
                       (file list, size and sha256 per file + the tar's size and sha256).
                       The tar's md5 and sha256 are computed WHILE it is written (a hashing
                       sink) — nothing is read back to prove the upload.
    3. rename        — `.partial` becomes `data_YYYYMMDD.tar`, manifest written.
    4. verify        — **rclone proof** (REQ-0034 amendment C): `rclone lsjson --hash
                       --files-only hl-mm-bot:hl-datasets/` must report an md5 for
                       `data_YYYYMMDD.tar` that equals the md5 of the bytes we wrote.
                       Retried every 30 s for up to 15 minutes (rclone's write cache can
                       delay the upload). Reading the file back through the mount is NOT
                       proof. Only `lsjson`, `md5sum`, `size`, `lsd` are ever invoked, and
                       only against `hl-mm-bot:hl-datasets/` — never delete/purge/sync/move,
                       never anything outside `hl-datasets/` on the remote.
    5. delete        — only after the md5 matches is the local day directory removed.

If ANY write check fails: the `.partial` artifacts this run created inside `hl-datasets/` are
removed, the local data is kept untouched, the failure is appended to `.agent/logs/ERRORS.log`,
and the run stops. If the remote md5 never appears/matches within the retry window: the LOCAL
data is kept and logged, and the already-written remote files are left alone for the human to
inspect — this script never deletes on the remote after the rename.

`--auto` (used by the collector every 5 minutes) only runs when
`shutil.disk_usage(project dir).free < 1 GiB` and stops archiving once free >= 2 GiB.

A lock file (`.archive_data.lock`, next to data/) prevents concurrent runs.
`--apply` runs re-exec themselves under `nice -n 10` + `ionice -c3` (once; guard env
ARCHIVE_NICED). Real runs never happen from this agent — only --dry-run and offline tests on
temp directories.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DEST_ROOT = Path("/root/gdrive_backup")
DEST_SUBDIR = "hl-datasets"

# remote verification (amendment C) — the ONLY remote this script ever talks to
RCLONE_REMOTE = "hl-mm-bot:hl-datasets/"
ALLOWED_RCLONE_SUBCOMMANDS = ("lsjson", "md5sum", "size", "lsd")
VERIFY_ATTEMPTS = 30  # 30 s apart -> last check at 14.5 min, i.e. "up to 15 minutes"
VERIFY_DELAY = 30.0

DAY_RE = re.compile(r"^\d{8}$")
PROBE_NAME = ".archive_write_test"
PROBE_BYTES = 1 << 20
CHUNK = 1 << 20
KEEP_CLOSED_DAYS = 2
AUTO_RUN_FREE = 1 << 30  # 1 GiB
AUTO_STOP_FREE = 2 << 30  # 2 GiB
BLOCKED_NAMES = ("raw_sample", "universe.json", "coins.json", "pre_sample.md")

# indirected so tests can mock the free-space trigger
disk_usage = shutil.disk_usage


class ArchiveError(RuntimeError):
    """Any refusal or verification failure."""


class MountSafetyError(ArchiveError):
    """Refused by the mount-safety gate, before anything was archived."""


# --------------------------------------------------------------------------- small helpers
def utc_today(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%d")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_size(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    i, x = 0, float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    return f"{int(x)} {units[i]}" if i == 0 else f"{x:.1f} {units[i]}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def log_error(msg: str, errors_log: Path) -> None:
    try:
        errors_log.parent.mkdir(parents=True, exist_ok=True)
        with errors_log.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now_utc()}] archive_data: {msg}\n")
    except OSError as exc:  # never mask the original failure
        print(f"WARNING: could not append to {errors_log}: {exc}", file=sys.stderr)


def assert_inside(data_dir: Path, path: Path) -> None:
    """Everything this script reads or deletes must live inside data/."""
    base = data_dir.resolve()
    target = path.resolve()
    if not target.is_relative_to(base):
        raise ArchiveError(f"refusing: {path} is outside {data_dir}")


# --------------------------------------------------------------------------- mount safety
def _st_dev(path: Path) -> int:
    return os.stat(path).st_dev


def mount_safety(
    dest_root: Path,
    data_dir: Path,
    *,
    dest_dir: Path | None = None,
    dest_dev: int | None = None,
    local_dev: int | None = None,
    probe_name: str = PROBE_NAME,
) -> None:
    """Refuse with a clear error unless every safety check passes.

    The 1 MiB probe is written INSIDE `dest_root/hl-datasets/` (amendment C: nothing is ever
    written at the mount root or anywhere else on the remote).
    """
    if not dest_root.is_dir():
        raise MountSafetyError(
            f"destination {dest_root} does not exist (mount it first) — refusing"
        )
    dest_dir = dest_dir or (dest_root / DEST_SUBDIR)

    local_ref = data_dir if data_dir.exists() else data_dir.parent
    if not local_ref.exists():
        raise MountSafetyError(f"local reference {local_ref} does not exist — refusing")

    dd = dest_dev if dest_dev is not None else _st_dev(dest_root)
    ld = local_dev if local_dev is not None else _st_dev(local_ref)
    if dd == ld:
        raise MountSafetyError(
            f"{dest_root} is on the SAME filesystem as {local_ref} (st_dev {dd}) — refusing: "
            "the archive destination must be a separate mount"
        )

    probe = dest_dir / probe_name
    payload = bytes(range(256)) * (PROBE_BYTES // 256)
    created = False
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with probe.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        created = True
        if probe.read_bytes() != payload:
            raise MountSafetyError(f"write test failed at {probe}: read-back mismatch")
    except OSError as exc:
        raise MountSafetyError(f"write test failed at {probe}: {exc}") from exc
    finally:
        if created:
            try:
                probe.unlink()
            except OSError as exc:
                print(f"WARNING: write test file could not be removed: {probe}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- eligibility
def closed_day_names(data_dir: Path) -> list[str]:
    if not data_dir.is_dir():
        return []
    names = [
        p.name
        for p in data_dir.iterdir()
        if p.is_dir() and DAY_RE.match(p.name) and p.name not in BLOCKED_NAMES
    ]
    names.sort()
    return names


def eligible_days(
    data_dir: Path, today: str | None = None, keep: int = KEEP_CLOSED_DAYS
) -> list[Path]:
    """Closed days strictly older than `today` (UTC), oldest first, minus the newest `keep`
    closed days which always stay local."""
    today = today or utc_today()
    days = [n for n in closed_day_names(data_dir) if n < today]
    keep_n = min(keep, len(days))
    kept = set(days[len(days) - keep_n :]) if keep_n else set()
    return [data_dir / n for n in days if n not in kept]


def should_auto_run(free: int) -> bool:
    return free < AUTO_RUN_FREE


def should_stop(free: int) -> bool:
    return free >= AUTO_STOP_FREE


# --------------------------------------------------------------------------- tar + manifest
def _walk(day: Path):
    """(dirs, files) sorted, symlinks not followed."""
    dirs, files = [], []
    for root, dnames, fnames in os.walk(day, followlinks=False):
        dnames.sort()
        fnames.sort()
        rp = Path(root)
        dirs.extend(rp / d for d in dnames)
        files.extend(rp / f for f in fnames)
    return dirs, files


def file_entries(day: Path) -> list[dict]:
    _dirs, files = _walk(day)
    entries = [
        {
            "path": f.relative_to(day).as_posix(),
            "size": f.stat().st_size,
            "sha256": sha256_file(f),
        }
        for f in files
    ]
    entries.sort(key=lambda e: e["path"])
    return entries


class _HashingWriter:
    """Wraps a binary file object and digests every byte written (no read-back needed)."""

    def __init__(self, fh):
        self._fh = fh
        self._md5 = hashlib.md5()
        self._sha = hashlib.sha256()

    def write(self, data):  # tarfile only needs write (+ the delegated tell/flush/...)
        self._md5.update(data)
        self._sha.update(data)
        return self._fh.write(data)

    @property
    def md5(self) -> str:
        return self._md5.hexdigest()

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    def __getattr__(self, name):  # pragma: no cover - delegation of tell/seek/flush/fileno
        return getattr(self._fh, name)


def write_tar(day: Path, partial: Path) -> tuple[str, str]:
    """Write the tar and return (md5, sha256) of exactly the bytes written to it."""
    dirs, files = _walk(day)
    with partial.open("wb") as fh:
        sink = _HashingWriter(fh)
        with tarfile.open(fileobj=sink, mode="w", format=tarfile.PAX_FORMAT) as tar:
            tar.add(day, arcname=day.name, recursive=False)
            for p in dirs + files:
                tar.add(p, arcname=p.relative_to(day.parent).as_posix(), recursive=False)
        fh.flush()
        return sink.md5, sink.sha256


# --------------------------------------------------------------------------- remote verification
def _assert_allowed_rclone_cmd(cmd: list[str]) -> None:
    """Hard gate: only lsjson/md5sum/size/lsd, only against hl-mm-bot:hl-datasets/ (rule 11 + C)."""
    if not cmd or cmd[0] != "rclone":
        raise ArchiveError(f"refusing to run a non-rclone command here: {cmd!r}")
    sub = next((c for c in cmd[1:] if not c.startswith("-")), "")
    if sub not in ALLOWED_RCLONE_SUBCOMMANDS:
        raise ArchiveError(
            f"rclone subcommand {sub!r} is not allowed (allowed: "
            f"{', '.join(ALLOWED_RCLONE_SUBCOMMANDS)}; never delete/purge/sync/move)"
        )
    for arg in cmd[2:]:
        if arg.startswith("-"):
            continue
        if arg != RCLONE_REMOTE:
            raise ArchiveError(
                f"refusing: rclone may only touch {RCLONE_REMOTE!r}, got {arg!r}"
            )


def rclone_lsjson(remote: str = RCLONE_REMOTE, *, runner=None) -> list[dict]:
    """`rclone lsjson --hash --files-only <remote>` -> the parsed listing (read-only)."""
    cmd = ["rclone", "lsjson", "--hash", "--files-only", remote]
    _assert_allowed_rclone_cmd(cmd)
    if runner is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            # rule 8: tests are offline and must never talk to the real remote
            raise ArchiveError("refusing to invoke rclone from inside a test")
        if shutil.which("rclone") is None:
            raise ArchiveError("rclone is not installed — cannot verify the remote upload")
    runner = runner or subprocess.run
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveError(f"rclone lsjson could not be run: {exc}") from exc
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()[:300]
        raise ArchiveError(f"rclone lsjson failed (exit {getattr(proc, 'returncode', 1)}): {err}")
    try:
        entries = json.loads(getattr(proc, "stdout", "") or "[]")
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"rclone lsjson returned unparseable output: {exc}") from exc
    if not isinstance(entries, list):
        raise ArchiveError(f"rclone lsjson returned {type(entries).__name__}, expected a list")
    return entries


def lsjson_md5_map(entries: list[dict] | None) -> dict[str, str]:
    """{file path (relative to the remote dir): lower-case md5} for entries that carry one."""
    out: dict[str, str] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("Path") or entry.get("Name")
        hashes = entry.get("Hashes") or {}
        if not isinstance(hashes, dict):
            continue
        md5 = hashes.get("md5") or hashes.get("MD5") or ""
        if path and isinstance(md5, str) and md5:
            out[path] = md5.lower()
    return out


def verify_remote_md5(
    filename: str,
    local_md5: str,
    *,
    attempts: int | None = None,
    delay: float | None = None,
    listing_fn=None,
    out=print,
) -> str:
    """Raise ArchiveError unless the remote md5 of `filename` equals `local_md5`.

    Retries every `delay` seconds (default 30 s) for `attempts` tries (default 30 = up to
    15 minutes) because rclone's write cache can delay the upload. Returns the matched md5.
    """
    attempts = VERIFY_ATTEMPTS if attempts is None else attempts
    delay = VERIFY_DELAY if delay is None else delay
    listing_fn = rclone_lsjson if listing_fn is None else listing_fn
    expected = (local_md5 or "").lower()
    if not expected or len(expected) != 32:
        raise ArchiveError(f"invalid local md5 for {filename}: {local_md5!r}")
    last = f"{filename} has not appeared in the remote listing yet"
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            remote_md5 = lsjson_md5_map(listing_fn()).get(filename)
        except ArchiveError as exc:
            remote_md5, last = None, str(exc)
        else:
            if remote_md5 is None:
                last = f"{filename} is not in {RCLONE_REMOTE} yet (rclone write cache may lag)"
            elif remote_md5 != expected:
                last = f"remote md5 {remote_md5} != local md5 {expected}"
            else:
                out(f"  verified    : {filename} md5 {expected} == remote "
                    f"(rclone lsjson, attempt {attempt}/{attempts})")
                return remote_md5
        if attempt < attempts:
            time.sleep(delay)
    raise ArchiveError(
        f"remote md5 verification failed for {filename} after {attempts} attempt(s) "
        f"({int(max(attempts - 1, 0) * delay)} s of retries): {last}"
    )


def _dest_paths(dest_dir: Path, day_name: str) -> tuple[Path, Path, Path, Path]:
    return (
        dest_dir / f"data_{day_name}.tar.partial",
        dest_dir / f"data_{day_name}.tar",
        dest_dir / f"{day_name}.manifest.json.partial",
        dest_dir / f"{day_name}.manifest.json",
    )


def _cleanup(created: list[Path]) -> None:
    for p in created:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# --------------------------------------------------------------------------- one day
def archive_day(
    day: Path,
    dest_dir: Path,
    *,
    data_dir: Path | None = None,
    errors_log: Path | None = None,
    out=print,
) -> dict:
    data_dir = data_dir or day.parent
    errors_log = errors_log or (data_dir.parent / ".agent" / "logs" / "ERRORS.log")
    assert_inside(data_dir, day)
    if not day.is_dir():
        raise ArchiveError(f"{day} is not a directory")
    if not DAY_RE.match(day.name):
        raise ArchiveError(f"{day.name} is not a YYYYMMDD directory — refusing")
    if day.name in BLOCKED_NAMES:
        raise ArchiveError(f"{day.name} is a protected name — refusing")

    partial, final, mf_partial, manifest = _dest_paths(dest_dir, day.name)
    created: list[Path] = []
    try:
        entries = file_entries(day)
        dest_dir.mkdir(parents=True, exist_ok=True)

        created.append(partial)
        tar_md5, tar_sha = write_tar(day, partial)  # digested while written — no read-back
        tar_size = partial.stat().st_size

        manifest_doc = {
            "day": day.name,
            "created_utc": now_utc(),
            "root": day.name,
            "file_count": len(entries),
            "total_bytes": sum(e["size"] for e in entries),
            "files": entries,
            "tar": {"name": final.name, "size": tar_size, "md5": tar_md5,
                    "sha256": tar_sha},
        }
        created.append(mf_partial)
        mf_partial.write_text(json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n")

        os.replace(partial, final)  # .partial -> final
        created.remove(partial)
        created.append(final)

        os.replace(mf_partial, manifest)
        created.remove(mf_partial)
        created.append(manifest)
    except Exception as exc:
        _cleanup(created)  # only the artifacts of THIS failed write, all inside hl-datasets/
        msg = f"archive of {day.name} failed: {exc}"
        log_error(msg, errors_log)
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError(msg) from exc

    # amendment C: proof that the bytes reached the remote — rclone md5, never a read-back
    try:
        verify_remote_md5(final.name, tar_md5, out=out)
    except ArchiveError as exc:
        # local data is KEPT (never deleted without a matching remote md5) and the remote
        # files are left in place: this script never deletes on the remote after the rename.
        msg = (
            f"archive of {day.name} written but the remote md5 check failed: {exc} | "
            f"local data kept; left on the remote for your inspection: {final.name}, "
            f"{manifest.name}"
        )
        log_error(msg, errors_log)
        raise ArchiveError(msg) from exc

    # the remote md5 matched — only now may the local day go
    try:
        shutil.rmtree(day)
    except OSError as exc:
        msg = (
            f"archive of {day.name} verified but the local day could not be removed: {exc} "
            "(both copies exist; nothing was deleted on the remote)"
        )
        log_error(msg, errors_log)
        raise ArchiveError(msg) from exc

    return {
        "day": day.name,
        "files": len(entries),
        "bytes": manifest_doc["total_bytes"],
        "tar_bytes": tar_size,
        "sha256": tar_sha,
        "md5": tar_md5,
    }


# --------------------------------------------------------------------------- lock
class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or "0")
                except (OSError, ValueError):
                    pid = 0
                if pid and self._pid_alive(pid):
                    raise ArchiveError(
                        f"another archive run is already in progress (lock {self.path}, "
                        f"pid {pid}) — refusing"
                    )
                # stale lock left by a crashed run
                try:
                    self.path.unlink()
                except OSError as exc:
                    raise ArchiveError(
                        f"stale lock {self.path} (pid {pid} is gone) could not be removed: {exc}"
                    ) from exc
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self.acquired = True
            return
        raise ArchiveError(f"could not acquire lock {self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False


# --------------------------------------------------------------------------- driver
def run(
    *,
    data_dir: Path,
    dest_root: Path,
    apply: bool = False,
    auto: bool = False,
    today: str | None = None,
    project_dir: Path | None = None,
    out=print,
) -> int:
    """Returns an exit code; the only side effects are inside dest_root / data_dir."""
    data_dir = data_dir.resolve()
    dest_root = dest_root.resolve()
    project_dir = (project_dir or data_dir.parent).resolve()
    dest_dir = dest_root / DEST_SUBDIR
    errors_log = project_dir / ".agent" / "logs" / "ERRORS.log"
    lock = Lock(project_dir / ".archive_data.lock")

    try:
        mount_safety(dest_root, data_dir, dest_dir=dest_dir)
    except MountSafetyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        log_error(str(exc), errors_log)
        return 2

    try:
        lock.acquire()
    except ArchiveError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    try:
        free_before = int(disk_usage(project_dir).free)
        mode = "APPLY" if apply else "DRY RUN"
        out(f"=== archive_data — {mode} {now_utc()} ===")
        out(f"  destination : {dest_dir}")
        out(f"  source      : {data_dir}")
        out(f"  mount safety: OK (st_dev {_st_dev(dest_root)} != local {_st_dev(project_dir)}, "
            f"1 MiB write test OK)")
        out(f"  free space  : {human_size(free_before)} "
            f"(auto: run < 1 GiB, stop >= 2 GiB)")

        if auto:
            if not should_auto_run(free_before):
                out(f"auto: free {human_size(free_before)} >= 1 GiB — nothing to do")
                return 0
            out(f"auto: free {human_size(free_before)} < 1 GiB — archiving eligible days")

        days = eligible_days(data_dir, today=today)
        if not days:
            if not data_dir.is_dir():
                reason = "data/ does not exist yet"
            else:
                reason = (
                    "no day strictly older than today (the newest "
                    f"{KEEP_CLOSED_DAYS} closed days always stay local)"
                )
            out(f"not eligible: {reason} — nothing to do")
            return 0

        out(f"  eligible    : {len(days)} day(s), oldest first")
        for d in days:
            out(f"    {d.name}  {human_size(dir_size(d))}")
        out(f"  retention   : newest {KEEP_CLOSED_DAYS} closed days always stay local")

        if not apply:
            out("dry-run: nothing was written, renamed or deleted.")
            return 0

        archived: list[dict] = []
        stopped_at: int | None = None
        try:
            for day in days:
                free_now = int(disk_usage(project_dir).free)
                if auto and should_stop(free_now):
                    stopped_at = free_now
                    out(f"stop: free {human_size(free_now)} >= 2 GiB — leaving the rest local")
                    break
                info = archive_day(
                    day, dest_dir, data_dir=data_dir, errors_log=errors_log, out=out
                )
                archived.append(info)
                out(f"  archived {info['day']}: {info['files']} file(s), "
                    f"{human_size(info['bytes'])} -> data_{info['day']}.tar "
                    f"({human_size(info['tar_bytes'])})")
        except ArchiveError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            out("stopped: the local data and any earlier verified days are kept as they are.")
            free_after = int(disk_usage(project_dir).free)
            out("--- summary ---")
            out(f"  archived days : {len(archived)}"
                + ("" if not archived else " (" + ", ".join(i["day"] for i in archived) + ")"))
            out(f"  bytes         : {human_size(sum(i['bytes'] for i in archived))} of source data")
            out(f"  free before   : {human_size(free_before)}")
            out(f"  free after    : {human_size(free_after)}")
            return 1

        free_after = int(disk_usage(project_dir).free)
        out("--- summary ---")
        out(f"  archived days : {len(archived)}"
            + ("" if not archived else " (" + ", ".join(i["day"] for i in archived) + ")"))
        out(f"  bytes         : {human_size(sum(i['bytes'] for i in archived))} of source data "
            f"({human_size(sum(i['tar_bytes'] for i in archived))} of tar)")
        out(f"  free before   : {human_size(free_before)}")
        out(f"  free after    : {human_size(free_after)}"
            + (f" (stopped at the 2 GiB mark)" if stopped_at else ""))
        return 0
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    ap = argparse.ArgumentParser(
        prog="archive_data.py",
        description="Archive closed data/YYYYMMDD days to the backup mount (dry-run by default).",
    )
    ap.add_argument("--apply", action="store_true",
                    help="actually archive (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op flag; the default is already a dry run")
    ap.add_argument("--auto", action="store_true",
                    help="only run when free < 1 GiB; stop archiving once free >= 2 GiB")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help=f"source directory (default {DEFAULT_DATA_DIR})")
    ap.add_argument("--dest-root", type=Path, default=DEFAULT_DEST_ROOT,
                    help=f"backup mount root (default {DEFAULT_DEST_ROOT})")
    args = ap.parse_args(argv)
    return run(
        data_dir=args.data_dir,
        dest_root=args.dest_root,
        apply=args.apply,
        auto=args.auto,
    )


def maybe_renice(argv: list[str]) -> None:
    """`nice -n 10 ionice -c3` for real (--apply) runs; guarded so it happens once."""
    if "--apply" not in argv or os.environ.get("ARCHIVE_NICED") == "1":
        return
    if not shutil.which("nice"):
        return
    cmd = ["nice", "-n", "10"]
    if shutil.which("ionice"):
        cmd += ["ionice", "-c3"]
    os.environ["ARCHIVE_NICED"] = "1"
    target = [sys.executable, str(Path(__file__).resolve()), *argv]
    print(f"  low priority : {' '.join(cmd)}")
    sys.stdout.flush()
    os.execvpe(cmd[0], cmd + target, os.environ)


if __name__ == "__main__":
    maybe_renice(sys.argv[1:])
    sys.exit(main())
