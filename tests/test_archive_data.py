"""Offline tests for scripts/archive_data.py + scripts/restore_day.py.

Temp directories only: every archive/extract/delete in these tests happens inside tmp_path
(or /dev/shm for the second-filesystem requirement). No network, no sudo, no mainnet.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import archive_data as ad  # noqa: E402
import restore_day  # noqa: E402

# captured at import time, BEFORE the autouse fake_remote fixture replaces ad.rclone_lsjson
REAL_RCLONE_LSJSON = ad.rclone_lsjson

GIB = 1 << 30


# --------------------------------------------------------------------------- helpers
def make_day(data_dir: Path, day: str, files: dict[str, bytes] | None = None) -> Path:
    d = data_dir / day
    d.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {"data.jsonl": f"{day} payload\n".encode()}).items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return d


def other_device_root(tmp_path: Path, name: str) -> Path:
    """A writable directory on a *different* st_dev than tmp_path (mount safety needs one)."""
    local_dev = os.stat(tmp_path).st_dev
    for cand in (Path("/dev/shm"), Path("/run/lock"), Path("/run")):
        try:
            if (
                cand.is_dir()
                and os.stat(cand).st_dev != local_dev
                and os.access(cand, os.W_OK)
            ):
                root = cand / f"archive_data_test_{os.getpid()}_{name}"
                shutil.rmtree(root, ignore_errors=True)
                root.mkdir(parents=True, exist_ok=True)
                return root
        except OSError:
            continue
    pytest.skip("no second filesystem available for the mount-safety tests")


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    dest = other_device_root(tmp_path, "dest")
    ns = types.SimpleNamespace(data=data, dest=dest, project=tmp_path)
    yield ns
    shutil.rmtree(dest, ignore_errors=True)


def run(env, **kw):
    kw.setdefault("data_dir", env.data)
    kw.setdefault("dest_root", env.dest)
    return ad.run(**kw)


# --------------------------------------------------------------------------- offline rclone fake
@pytest.fixture(autouse=True)
def fake_remote(monkeypatch):
    """Offline guarantee (rule 8): no test may ever invoke the real rclone.

    The fake listing reports the md5 of the .tar files that archive_day actually wrote —
    i.e. it simulates an upload that succeeded. Tests that want a mismatch or a missing
    file simply monkeypatch `ad.rclone_lsjson` again in the test body.
    """
    seen: dict[str, object] = {"dest": None}
    real_archive_day = ad.archive_day

    def spy(day, dest_dir, **kw):
        seen["dest"] = dest_dir
        return real_archive_day(day, dest_dir, **kw)

    monkeypatch.setattr(ad, "archive_day", spy)

    def fake_listing(remote: str = ad.RCLONE_REMOTE, *, runner=None):
        dest = seen["dest"]
        entries = []
        if dest is not None and Path(dest).is_dir():
            for f in sorted(Path(dest).glob("*.tar")):
                entries.append(
                    {
                        "Path": f.name,
                        "Name": f.name,
                        "Size": f.stat().st_size,
                        "Hashes": {"md5": hashlib.md5(f.read_bytes()).hexdigest()},
                    }
                )
        return entries

    monkeypatch.setattr(ad, "rclone_lsjson", fake_listing)
    return fake_listing


# --------------------------------------------------------------------------- mount safety
def test_refuses_same_st_dev(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    dest = tmp_path / "gdrive_backup"  # same filesystem as data/
    dest.mkdir()
    with pytest.raises(ad.MountSafetyError, match="SAME filesystem"):
        ad.mount_safety(dest, data)


def test_refuses_missing_destination(tmp_path):
    with pytest.raises(ad.MountSafetyError, match="does not exist"):
        ad.mount_safety(tmp_path / "no_such_mount", tmp_path)


def test_refuses_when_write_test_fails(env):
    probe = env.dest / ad.DEST_SUBDIR / ad.PROBE_NAME
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.mkdir()  # a directory sits where the 1 MiB probe file must be written
    with pytest.raises(ad.MountSafetyError, match="write test failed"):
        ad.mount_safety(env.dest, env.data)
    assert probe.is_dir()  # the pre-existing path was not removed


def test_write_probe_is_removed_after_success(env):
    ad.mount_safety(env.dest, env.data)
    # the probe lives INSIDE hl-datasets and is gone afterwards (nothing at the mount root)
    assert not (env.dest / ad.DEST_SUBDIR / ad.PROBE_NAME).exists()
    assert not (env.dest / ad.PROBE_NAME).exists()


# --------------------------------------------------------------------------- failures
def _three_days(env) -> Path:
    """Two kept days + the eligible oldest one; returns the eligible day."""
    day = make_day(env.data, "20260101")
    make_day(env.data, "20260102")
    make_day(env.data, "20260103")  # newest 2 closed days stay local, 01 is eligible
    return day


def test_write_failure_removes_the_partial_and_keeps_local_data(env, monkeypatch):
    day = _three_days(env)

    def boom(day_, partial):
        partial.write_bytes(b"half a tar")
        raise ad.ArchiveError("disk full while writing")

    monkeypatch.setattr(ad, "write_tar", boom)
    assert run(env, apply=True, today="20260110") == 1
    assert day.is_dir() and (day / "data.jsonl").is_file()  # local data untouched
    dest_dir = env.dest / ad.DEST_SUBDIR
    assert list(dest_dir.glob("*.partial")) == []  # this run's half-write is removed
    assert list(dest_dir.glob("data_*.tar")) == []
    assert list(dest_dir.glob("*.manifest.json")) == []
    errs = env.project / ".agent" / "logs" / "ERRORS.log"
    assert errs.is_file() and "disk full while writing" in errs.read_text()


def test_remote_md5_mismatch_keeps_local_data_and_leaves_the_remote_alone(env, monkeypatch):
    day = _three_days(env)
    monkeypatch.setattr(ad, "VERIFY_ATTEMPTS", 3)
    monkeypatch.setattr(ad, "VERIFY_DELAY", 0.0)

    def mismatch(remote=ad.RCLONE_REMOTE, *, runner=None):
        return [{"Path": "data_20260101.tar", "Size": 4, "Hashes": {"md5": "0" * 32}}]

    monkeypatch.setattr(ad, "rclone_lsjson", mismatch)
    assert run(env, apply=True, today="20260110") == 1
    assert day.is_dir() and (day / "data.jsonl").is_file()  # local data KEPT (rule: md5 first)
    dest_dir = env.dest / ad.DEST_SUBDIR
    # nothing is ever deleted on the remote after the rename (amendment C)
    assert [p.name for p in sorted(dest_dir.glob("data_*.tar"))] == ["data_20260101.tar"]
    assert (dest_dir / "20260101.manifest.json").is_file()
    errs = env.project / ".agent" / "logs" / "ERRORS.log"
    text = errs.read_text()
    assert "remote md5 check failed" in text
    assert "left on the remote" in text
    assert "local data kept" in text


def test_remote_md5_missing_from_listing_keeps_local_data(env, monkeypatch):
    day = _three_days(env)
    monkeypatch.setattr(ad, "VERIFY_ATTEMPTS", 3)
    monkeypatch.setattr(ad, "VERIFY_DELAY", 0.0)
    calls = {"n": 0}

    def missing(remote=ad.RCLONE_REMOTE, *, runner=None):
        calls["n"] += 1
        return [{"Path": "other.tar", "Size": 9, "Hashes": {"md5": "1" * 32}}]

    monkeypatch.setattr(ad, "rclone_lsjson", missing)
    assert run(env, apply=True, today="20260110") == 1
    assert calls["n"] == 3  # retried VERIFY_ATTEMPTS times
    assert day.is_dir() and (day / "data.jsonl").is_file()
    assert "not in hl-mm-bot:hl-datasets/" in (env.project / ".agent" / "logs" / "ERRORS.log").read_text()


# --------------------------------------------------------------------------- eligibility
def test_never_deletes_today(env):
    oldest = make_day(env.data, "20260107")
    make_day(env.data, "20260108")
    make_day(env.data, "20260109")
    today = make_day(env.data, "20260110", {"live.jsonl": b"today must survive\n"})
    assert run(env, apply=True, today="20260110") == 0
    assert not oldest.exists()  # archived (eligible)
    assert (env.data / "20260108").is_dir() and (env.data / "20260109").is_dir()
    assert today.is_dir() and (today / "live.jsonl").read_bytes() == b"today must survive\n"


def test_never_touches_paths_outside_data(env):
    make_day(env.data, "20260101")
    make_day(env.data, "20260102")
    make_day(env.data, "20260103")  # keeps 02 + 03 local, archives 01
    raw = env.data / "raw_sample"
    raw.mkdir()
    (raw / "x.raw").write_bytes(b"raw")
    for name in ("universe.json", "coins.json", "pre_sample.md"):
        (env.data / name).write_text(name)
    outside = env.project / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("canary")
    dest_canary = env.dest / "canary.txt"
    dest_canary.write_text("dest canary")

    assert run(env, apply=True, today="20261001") == 0

    assert not (env.data / "20260101").exists()
    assert (env.data / "20260102").is_dir() and (env.data / "20260103").is_dir()
    assert (raw / "x.raw").read_bytes() == b"raw"
    for name in ("universe.json", "coins.json", "pre_sample.md"):
        assert (env.data / name).read_text() == name
    assert canary.read_text() == "canary"
    assert dest_canary.read_text() == "dest canary"
    # nothing was created outside data/, dest/hl-datasets and the (released) lock
    assert sorted(p.name for p in env.project.iterdir()) == ["data", "outside"]


def test_keeps_newest_two_closed_days_local(env):
    days = [f"2026010{i}" for i in range(1, 6)]
    for d in days:
        make_day(env.data, d)

    assert run(env, apply=True, today="20260106") == 0

    assert sorted(p.name for p in env.data.iterdir()) == ["20260104", "20260105"]
    dest_dir = env.dest / ad.DEST_SUBDIR
    assert sorted(p.name for p in dest_dir.glob("data_*.tar")) == [
        "data_20260101.tar",
        "data_20260102.tar",
        "data_20260103.tar",
    ]
    assert sorted(p.name for p in dest_dir.glob("*.manifest.json")) == [
        "20260101.manifest.json",
        "20260102.manifest.json",
        "20260103.manifest.json",
    ]
    assert list(dest_dir.glob("*.partial")) == []
    mf = json.loads((dest_dir / "20260101.manifest.json").read_text())
    assert mf["day"] == "20260101"
    assert mf["file_count"] == 1
    assert mf["files"][0]["path"] == "data.jsonl"
    assert len(mf["files"][0]["sha256"]) == 64
    assert mf["tar"]["name"] == "data_20260101.tar"
    assert len(mf["tar"]["sha256"]) == 64


def test_dry_run_reports_not_eligible_and_writes_nothing(tmp_path):
    data = tmp_path / "data"  # data/ does not exist yet
    dest = other_device_root(tmp_path, "dry")
    try:
        rc = ad.run(data_dir=data, dest_root=dest, apply=False)
        assert rc == 0
        assert not (dest / ad.PROBE_NAME).exists()  # nothing is ever written at the root
        if (dest / ad.DEST_SUBDIR).exists():
            assert list((dest / ad.DEST_SUBDIR).iterdir()) == []
        assert sorted(p.name for p in tmp_path.iterdir()) == []
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_dry_run_lists_days_but_archives_nothing(env):
    make_day(env.data, "20260101")
    make_day(env.data, "20260102")
    assert run(env, apply=False, today="20260110") == 0
    assert (env.data / "20260101").is_dir() and (env.data / "20260102").is_dir()
    assert not (env.dest / ad.DEST_SUBDIR).exists() or (
        list((env.dest / ad.DEST_SUBDIR).iterdir()) == []
    )


# --------------------------------------------------------------------------- free-space trigger
def test_free_space_threshold_units():
    assert ad.should_auto_run(GIB - 1) is True
    assert ad.should_auto_run(GIB) is False
    assert ad.should_auto_run(int(0.9 * GIB)) is True
    assert ad.should_stop(2 * GIB - 1) is False
    assert ad.should_stop(2 * GIB) is True


def test_auto_trigger_thresholds_mocked(env, monkeypatch):
    for d in ("20260101", "20260102", "20260103"):
        make_day(env.data, d)

    # free >= 1 GiB -> --auto does nothing at all
    monkeypatch.setattr(
        ad,
        "disk_usage",
        lambda _p: types.SimpleNamespace(free=int(1.5 * GIB), total=0, used=0),
    )
    assert run(env, apply=True, auto=True, today="20260106") == 0
    assert sorted(p.name for p in env.data.iterdir()) == [
        "20260101",
        "20260102",
        "20260103",
    ]
    # the mount-safety probe may have created (and emptied) hl-datasets — nothing else
    if (env.dest / ad.DEST_SUBDIR).exists():
        assert list((env.dest / ad.DEST_SUBDIR).iterdir()) == []
    assert not (env.dest / ad.PROBE_NAME).exists()

    # free < 1 GiB -> archive, then stop as soon as free >= 2 GiB
    calls = {"n": 0}
    seq = [int(0.9 * GIB), int(0.9 * GIB), int(2.5 * GIB)]

    def fake_usage(_p):
        calls["n"] += 1
        return types.SimpleNamespace(free=seq[min(calls["n"] - 1, len(seq) - 1)], total=0, used=0)

    monkeypatch.setattr(ad, "disk_usage", fake_usage)
    assert run(env, apply=True, auto=True, today="20260106") == 0
    assert [p.name for p in sorted(env.data.iterdir())] == ["20260102", "20260103"]
    dest_dir = env.dest / ad.DEST_SUBDIR
    assert [p.name for p in sorted(dest_dir.glob("data_*.tar"))] == ["data_20260101.tar"]


# --------------------------------------------------------------------------- lock
def test_lock_blocks_a_second_concurrent_run(env, monkeypatch):
    lock = env.project / ".archive_data.lock"
    lock.write_text(str(os.getpid()))  # a live holder
    assert run(env, apply=False, today="20260110") == 3
    assert lock.exists()  # the holder's lock was NOT stolen
    lock.unlink()

    # a stale lock (dead pid) is broken instead of blocking forever
    monkeypatch.setattr(ad.Lock, "_pid_alive", staticmethod(lambda _pid: False))
    lock.write_text("12345")
    assert run(env, apply=False, today="20260110") == 0
    assert not lock.exists()


# --------------------------------------------------------------------------- restore
def test_restore_roundtrip(env):
    make_day(
        env.data,
        "20260101",
        {"a.jsonl": b"line1\nline2\n", "sub/b.bin": b"\x00\x01\x02"},
    )
    make_day(env.data, "20260102")
    make_day(env.data, "20260103")  # newest 2 closed days stay local, 01 is eligible
    assert run(env, apply=True, today="20260106") == 0
    assert not (env.data / "20260101").exists()

    args = ["20260101", "--data-dir", str(env.data), "--dest-root", str(env.dest)]
    assert restore_day.main(args) == 0  # dry run: verifies the tar, writes nothing
    assert not (env.data / "20260101").exists()

    assert restore_day.main(args + ["--apply"]) == 0
    assert (env.data / "20260101" / "a.jsonl").read_bytes() == b"line1\nline2\n"
    assert (env.data / "20260101" / "sub" / "b.bin").read_bytes() == b"\x00\x01\x02"

    # restoring again refuses to overwrite the local day
    assert restore_day.main(args + ["--apply"]) == 2


# --------------------------------------------------------------------------- amendment C: rclone md5
def test_lsjson_md5_map_parses_only_entries_with_an_md5():
    entries = [
        {"Path": "a.tar", "Hashes": {"md5": "A" * 32, "sha1": "b" * 40}},  # case folded
        {"Path": "b.tar", "Hashes": {"MD5": "c" * 32}},
        {"Path": "c.tar", "Hashes": {}},          # no hash -> not verifiable
        {"Path": "d.tar"},                         # no Hashes key at all
        {"Name": "e.tar", "Hashes": {"md5": "d" * 32}},
        "not-a-dict",
        {"Path": "", "Hashes": {"md5": "e" * 32}},  # empty path
    ]
    m = ad.lsjson_md5_map(entries)
    assert m == {"a.tar": "a" * 32, "b.tar": "c" * 32, "e.tar": "d" * 32}
    assert ad.lsjson_md5_map(None) == {}
    assert ad.lsjson_md5_map([]) == {}


def test_verify_remote_md5_matches_on_the_first_attempt():
    seen = {"n": 0}
    local = "f" * 32

    def listing(remote=ad.RCLONE_REMOTE, *, runner=None):
        seen["n"] += 1
        return [{"Path": "data_x.tar", "Hashes": {"md5": local.upper()}}]

    out = []
    assert ad.verify_remote_md5(
        "data_x.tar", local, attempts=5, delay=0.0, listing_fn=listing, out=out.append
    ) == local
    assert seen["n"] == 1  # no pointless retries once the md5 matches
    assert out and "verified" in out[0] and "data_x.tar" in out[0]


def test_verify_remote_md5_mismatch_retries_the_configured_number_of_times():
    seen = {"n": 0}

    def listing(remote=ad.RCLONE_REMOTE, *, runner=None):
        seen["n"] += 1
        return [{"Path": "data_x.tar", "Hashes": {"md5": "0" * 32}}]

    with pytest.raises(ad.ArchiveError) as exc:
        ad.verify_remote_md5("data_x.tar", "f" * 32, attempts=3, delay=0.0, listing_fn=listing)
    assert seen["n"] == 3
    assert "remote md5 00000000" in str(exc.value) and "local md5 ffffff" in str(exc.value)
    assert "after 3 attempt(s)" in str(exc.value)


def test_verify_remote_md5_missing_entry_retries_then_fails():
    seen = {"n": 0}

    def listing(remote=ad.RCLONE_REMOTE, *, runner=None):
        seen["n"] += 1
        return []  # upload has not appeared yet

    with pytest.raises(ad.ArchiveError, match="not in hl-mm-bot:hl-datasets/"):
        ad.verify_remote_md5("data_x.tar", "f" * 32, attempts=2, delay=0.0, listing_fn=listing)
    assert seen["n"] == 2


def test_verify_remote_md5_entry_without_a_hash_counts_as_missing():
    def listing(remote=ad.RCLONE_REMOTE, *, runner=None):
        return [{"Path": "data_x.tar", "Size": 5, "Hashes": {}}]

    with pytest.raises(ad.ArchiveError, match="not in hl-mm-bot:hl-datasets/"):
        ad.verify_remote_md5("data_x.tar", "f" * 32, attempts=1, delay=0.0, listing_fn=listing)


def test_verify_remote_md5_rejects_an_invalid_local_md5_without_calling_rclone():
    def listing(remote=ad.RCLONE_REMOTE, *, runner=None):
        raise AssertionError("must not be called for an invalid local md5")

    for bad in ("", "xyz", "f" * 31, None):
        with pytest.raises(ad.ArchiveError, match="invalid local md5"):
            ad.verify_remote_md5("data_x.tar", bad, attempts=1, delay=0.0, listing_fn=listing)


def test_verify_window_is_30s_retries_for_up_to_15_minutes():
    assert ad.VERIFY_ATTEMPTS == 30
    assert ad.VERIFY_DELAY == 30.0
    # first check immediately, then 29 more 30 s apart = 870 s of retries (<= 15 minutes)
    assert (ad.VERIFY_ATTEMPTS - 1) * ad.VERIFY_DELAY == 870


def test_rclone_cmd_guard_allows_only_the_four_read_only_subcommands():
    for allowed in ad.ALLOWED_RCLONE_SUBCOMMANDS:
        ad._assert_allowed_rclone_cmd(["rclone", allowed, ad.RCLONE_REMOTE])
    for forbidden in ("delete", "deletefile", "purge", "sync", "move", "copy", "rcat",
                      "mkdir", "rmdirs", "touch", "backend", "config", "serve"):
        with pytest.raises(ad.ArchiveError, match="not allowed"):
            ad._assert_allowed_rclone_cmd(["rclone", forbidden, ad.RCLONE_REMOTE])
    # a non-rclone binary is refused too
    with pytest.raises(ad.ArchiveError, match="non-rclone"):
        ad._assert_allowed_rclone_cmd(["/bin/rm", "-rf", "/"])


def test_rclone_cmd_guard_refuses_anything_outside_hl_datasets():
    for remote in ("hl-mm-bot:", "hl-mm-bot:other/", "gdrive:hl-datasets/", "/etc",
                   "hl-mm-bot:hl-datasets/../secrets", ad.RCLONE_REMOTE + "x/"):
        with pytest.raises(ad.ArchiveError, match="may only touch"):
            ad._assert_allowed_rclone_cmd(["rclone", "lsjson", "--hash", "--files-only", remote])
    with pytest.raises(ad.ArchiveError, match="may only touch"):
        ad._assert_allowed_rclone_cmd(["rclone", "lsjson", ad.RCLONE_REMOTE, "/root"])


def test_rclone_lsjson_runs_exactly_the_allowed_command():
    seen: dict = {}

    def runner(cmd, capture_output=True, text=True, timeout=None):
        seen["cmd"] = cmd
        return types.SimpleNamespace(
            returncode=0,
            stdout='[{"Path": "data_x.tar", "Size": 7, "Hashes": {"md5": "' + "a" * 32 + '"}}]',
            stderr="",
        )

    entries = REAL_RCLONE_LSJSON(runner=runner)
    assert seen["cmd"] == [
        "rclone", "lsjson", "--hash", "--files-only", "hl-mm-bot:hl-datasets/"
    ]
    assert ad.lsjson_md5_map(entries) == {"data_x.tar": "a" * 32}


def test_rclone_lsjson_surfaces_errors_instead_of_returning_junk():
    def failing(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom: no such remote")

    with pytest.raises(ad.ArchiveError, match="rclone lsjson failed"):
        REAL_RCLONE_LSJSON(runner=failing)

    def bad_json(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout="{not json", stderr="")

    with pytest.raises(ad.ArchiveError, match="unparseable"):
        REAL_RCLONE_LSJSON(runner=bad_json)

    def not_a_list(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout='{"Path": "x"}', stderr="")

    with pytest.raises(ad.ArchiveError, match="expected a list"):
        REAL_RCLONE_LSJSON(runner=not_a_list)


def test_source_builds_only_allowed_rclone_commands():
    """Static guard: every rclone command literal in the script is read-only and scoped."""
    import re

    src = Path(ad.__file__).read_text()
    blocks = re.findall(r"\[([^\[\]]*\"rclone\"[^\[\]]*)\]", src)
    assert blocks, "expected at least one rclone command literal in archive_data.py"
    for block in blocks:
        tokens = re.findall(r'"([^"]+)"', block)
        assert tokens and tokens[0] == "rclone", tokens
        positional = [t for t in tokens[1:] if not t.startswith("-")]
        flags = [t for t in tokens[1:] if t.startswith("-")]
        assert positional and positional[0] in ad.ALLOWED_RCLONE_SUBCOMMANDS, tokens
        assert set(flags) <= {"--hash", "--files-only"}, tokens
        assert all(p == ad.RCLONE_REMOTE for p in positional[1:]), tokens


def test_manifest_carries_md5_and_local_day_survives_until_it_matches(env):
    day = make_day(env.data, "20260101")
    make_day(env.data, "20260102")
    make_day(env.data, "20260103")
    assert run(env, apply=True, today="20260110") == 0
    dest_dir = env.dest / ad.DEST_SUBDIR
    tar = dest_dir / "data_20260101.tar"
    assert tar.is_file()
    mf = json.loads((dest_dir / "20260101.manifest.json").read_text())
    assert mf["tar"]["md5"] == hashlib.md5(tar.read_bytes()).hexdigest()
    assert mf["tar"]["sha256"] == hashlib.sha256(tar.read_bytes()).hexdigest()
    assert not day.exists()  # deleted only because the remote md5 matched
