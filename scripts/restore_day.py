#!/usr/bin/env python3
"""scripts/restore_day.py YYYYMMDD — unpack one archived day into data/ and verify it.

DRY-RUN BY DEFAULT: `--apply` is required to write anything. Steps:

    1. read  <dest>/hl-datasets/YYYYMMDD.manifest.json  (file list, sizes, sha256 per file,
       tar size + sha256)
    2. verify data_YYYYMMDD.tar against the manifest's sha256
    3. dry-run: print the plan and stop
       apply  : untar into data/ (safe `filter="data"`, never outside data/), refuse if the
       day is already present locally, then verify every extracted file against the manifest.

Any failure removes only the directory this run just created, keeps everything else, appends
to .agent/logs/ERRORS.log and exits non-zero.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_data as ad  # noqa: E402


def log_error(msg: str, errors_log: Path) -> None:
    ad.log_error(msg, errors_log)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    ap = argparse.ArgumentParser(
        prog="restore_day.py",
        description="Restore one archived YYYYMMDD day into data/ and verify it.",
    )
    ap.add_argument("day", help="YYYYMMDD day to restore")
    ap.add_argument("--apply", action="store_true", help="actually extract (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit no-op flag (default)")
    ap.add_argument("--data-dir", type=Path, default=ad.DEFAULT_DATA_DIR)
    ap.add_argument("--dest-root", type=Path, default=ad.DEFAULT_DEST_ROOT)
    args = ap.parse_args(argv)

    day = args.day
    data_dir = args.data_dir.resolve()
    dest_root = args.dest_root.resolve()
    project_dir = data_dir.parent
    errors_log = project_dir / ".agent" / "logs" / "ERRORS.log"

    if not ad.DAY_RE.match(day):
        print(f"ERROR: {day!r} is not a YYYYMMDD day", file=sys.stderr)
        return 2

    dest_dir = dest_root / ad.DEST_SUBDIR
    tar_path = dest_dir / f"data_{day}.tar"
    manifest_path = dest_dir / f"{day}.manifest.json"

    if not dest_root.is_dir():
        print(f"ERROR: destination {dest_root} does not exist", file=sys.stderr)
        return 2
    if not manifest_path.is_file() or not tar_path.is_file():
        print(f"ERROR: no archive for {day} under {dest_dir} "
              f"(manifest={'yes' if manifest_path.is_file() else 'NO'}, "
              f"tar={'yes' if tar_path.is_file() else 'NO'})", file=sys.stderr)
        return 2

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"manifest {manifest_path.name} is unreadable: {exc}"
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1

    expected = manifest.get("tar", {}).get("sha256", "")
    if manifest.get("day") != day:
        msg = f"manifest day {manifest.get('day')!r} does not match {day}"
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1

    files = manifest.get("files", [])
    print(f"=== restore_day {day} — {'APPLY' if args.apply else 'DRY RUN'} {ad.now_utc()} ===")
    print(f"  tar      : {tar_path} ({ad.human_size(tar_path.stat().st_size)})")
    print(f"  manifest : {manifest_path.name} ({len(files)} file(s))")

    try:
        actual = ad.sha256_file(tar_path)
    except OSError as exc:
        msg = f"cannot read {tar_path}: {exc}"
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1
    if actual != expected:
        msg = (f"checksum mismatch for {tar_path.name}: manifest says {expected}, "
               f"file reads {actual}")
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1
    print(f"  tar sha256: verified ({expected[:16]}…)")

    # every member must stay under data/<day>/
    try:
        with tarfile.open(tar_path, "r:") as tar:
            members = tar.getmembers()
    except tarfile.TarError as exc:
        msg = f"{tar_path.name} is not a readable tar: {exc}"
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1
    bad = [m.name for m in members
           if not (m.name == day or m.name.startswith(day + "/"))]
    if bad:
        msg = f"{tar_path.name} contains members outside {day}/: {bad[:3]}"
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1

    target = data_dir / day
    if target.exists():
        print(f"  local day {target} already exists — refusing to overwrite", file=sys.stderr)
        return 2
    if not args.apply:
        print(f"  would extract {len(members)} member(s) into {target}")
        print("dry-run: nothing was written.")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    ad.assert_inside(data_dir, target)
    created = False
    try:
        with tarfile.open(tar_path, "r:") as tar:
            tar.extractall(data_dir, members=members, filter="data")
        created = True
        for entry in files:
            path = target / entry["path"]
            if not path.is_file():
                raise ad.ArchiveError(f"missing after extract: {entry['path']}")
            if path.stat().st_size != entry["size"]:
                raise ad.ArchiveError(f"size mismatch after extract: {entry['path']}")
            if ad.sha256_file(path) != entry["sha256"]:
                raise ad.ArchiveError(f"sha256 mismatch after extract: {entry['path']}")
    except Exception as exc:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        msg = f"restore of {day} failed: {exc}"
        print(f"ERROR: {msg}", file=sys.stderr)
        log_error(msg, errors_log)
        return 1

    total = sum(e["size"] for e in files)
    print(f"  restored {len(files)} file(s), {ad.human_size(total)} -> {target}")
    print(f"  verified against {manifest_path.name} at "
          f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
