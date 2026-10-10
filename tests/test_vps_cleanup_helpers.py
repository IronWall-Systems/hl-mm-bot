"""Pure helper functions of scripts/vps_cleanup.sh (REQ-0033 / T9b).

Offline only: the script is *sourced* (its `main` is guarded by BASH_SOURCE == $0, so nothing
runs) and the pure helpers are called. No network, no file mutation, no docker/apt/journal calls.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vps_cleanup.sh"


def _yes_no(expr: str) -> str:
    """Run a bash expression under the script's helpers; return YES/NO for a boolean call."""
    cmd = f'source "{SCRIPT}"; if {expr}; then echo YES; else echo NO; fi'
    out = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _value(expr: str) -> str:
    cmd = f'source "{SCRIPT}"; {expr}'
    out = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_script_exists_and_is_bash():
    assert SCRIPT.is_file()
    first = SCRIPT.read_text().splitlines()[0]
    assert "bash" in first


def test_human_size_zero_and_small():
    assert _value("human_size 0") == "0 B"
    assert _value("human_size 512") == "512 B"


def test_human_size_kib_mib_gib():
    assert _value("human_size 1024") == "1.0 KiB"
    assert _value("human_size 1572864") == "1.5 MiB"
    assert _value("human_size 5368709120") == "5.0 GiB"


def test_is_protected_log_current_run_files():
    assert _yes_no("is_protected_log bot.log") == "YES"
    assert _yes_no("is_protected_log bot.log.3") == "YES"      # rotating-file backup
    assert _yes_no("is_protected_log soak_monitor.log") == "YES"
    assert _yes_no("is_protected_log soak_bot.log") == "YES"    # live soak wrapper
    assert _yes_no("is_protected_log soak_monitor.out") == "YES"
    assert _yes_no("is_protected_log soak_pids.txt") == "YES"
    assert _yes_no("is_protected_log /root/hl-mm-bot/logs/bot.log") == "YES"  # path form


def test_is_protected_log_allows_archived_run_files():
    assert _yes_no("is_protected_log bot_t8c.log") == "NO"
    assert _yes_no("is_protected_log soak_bot_t8b.log") == "NO"  # archived soak copy
    assert _yes_no("is_protected_log soak_pids_t8a.txt") == "NO"  # archived soak copy
    assert _yes_no("is_protected_log dryrun_t5.log") == "NO"


def test_is_clean_word_gate():
    assert _yes_no("is_clean_word CLEAN") == "YES"
    assert _yes_no("is_clean_word clean") == "NO"      # case sensitive
    assert _yes_no("is_clean_word CLEANED") == "NO"
    assert _yes_no('is_clean_word ""') == "NO"
    assert _yes_no("is_clean_word") == "NO"            # unset (set -u safe)


# --- REQ-0034 amendment B (Stage 1 additions: --vscode-old, --caches, process guard) ---------
import os
import time

import pytest


def _value_with(expr: str, **assign: str) -> str:
    """Source the script, override its variables (fake paths), then run expr."""
    pre = " ".join(f'{k}="{v}"' for k, v in assign.items())
    cmd = f'source "{SCRIPT}"; {pre}; {expr}'
    out = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _mk_bin(tmp_path, names):
    """Create bin/<commit> dirs with increasing mtimes and sizes; return their paths."""
    paths = []
    for i, name in enumerate(names):
        d = tmp_path / name
        d.mkdir()
        (d / "code").write_bytes(b"x" * (100 * (i + 1)))
        stamp = 1_700_000_000 + i * 60
        os.utime(d, (stamp, stamp))
        paths.append(d)
    return paths


def test_usage_lists_the_new_flags():
    out = _value("usage")
    for flag in ("--vscode-old", "--caches", "--report", "--project-logs", "--apply"):
        assert flag in out, flag


def test_vscode_bin_dirs_is_three_fields_oldest_first(tmp_path):
    first, second = _mk_bin(tmp_path, ["aaa111", "bbb222"])
    out = _value_with("vscode_bin_dirs", VSCODE_BIN=str(tmp_path))
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 2
    epochs = []
    for line in lines:
        epoch, size, path = line.split("\t")
        epochs.append(int(epoch))
        assert Path(path).is_dir()
        assert int(size) > 0
    assert epochs == sorted(epochs)
    assert lines[0].split("\t")[2] == str(first)
    assert lines[1].split("\t")[2] == str(second)


def test_vscode_old_dirs_drops_only_the_newest(tmp_path):
    a, b, c = _mk_bin(tmp_path, ["a1", "b2", "c3"])
    out = _value_with("vscode_old_dirs", VSCODE_BIN=str(tmp_path))
    olds = [line for line in out.splitlines() if line]
    assert str(b) in olds and str(a) in olds
    assert str(c) not in olds  # newest is kept, never offered for deletion


def test_vscode_old_dirs_empty_when_missing_or_single(tmp_path):
    assert _value_with("vscode_old_dirs", VSCODE_BIN=str(tmp_path)) == ""
    _mk_bin(tmp_path, ["only1"])
    assert _value_with("vscode_old_dirs", VSCODE_BIN=str(tmp_path)) == ""


def test_plan_vscode_old_prints_full_list_and_gates_on_apply(tmp_path):
    old, newest = _mk_bin(tmp_path, ["old1", "new2"])
    out = _value_with("plan_vscode_old", VSCODE_BIN=str(tmp_path))
    assert "full list" in out
    assert "KEEP" in out
    assert str(newest) in out
    if "REFUSED" in out:  # a vscode-server process is alive on this machine
        assert "rm -rf" not in out
    else:
        assert str(old) in out
        assert "rm -rf -- '" in out
        assert "(dry-run: not executed)" in out  # plan mode never executes


def test_plan_caches_lists_cache_dir_and_never_deletes_it(tmp_path):
    cache = tmp_path / "cache"
    (cache / "somecache").mkdir(parents=True)
    (cache / "somecache" / "blob").write_bytes(b"x" * 32)
    out = _value_with(
        "plan_caches", CACHE_DIR=str(cache), NPM_DIR=str(tmp_path / "nonpm")
    )
    assert "somecache" in out  # listed for manual review
    assert "rm " not in out and "rm -rf" not in out  # /root/.cache is never deleted
    assert "(dry-run: not executed)" in out or "skipped" in out


def test_list_cache_dirs_reads_only_the_given_dir(tmp_path):
    (tmp_path / "pip").mkdir()
    (tmp_path / "pip" / "f").write_bytes(b"x" * 4096)
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm" / "f").write_bytes(b"x" * 16)
    out = _value_with("list_cache_dirs", CACHE_DIR=str(tmp_path))
    assert "pip" in out and "npm" in out
    assert _value_with("list_cache_dirs", CACHE_DIR=str(tmp_path / "nope")) == ""


def test_process_matches_never_matches_itself_or_a_missing_process():
    assert _yes_no("which_process_matches vps_cleanup.sh") == "NO"  # self-filtered
    assert _yes_no("which_process_matches no_such_proc_9f3a2b1c") == "NO"


def test_process_matches_finds_a_live_process():
    proc = subprocess.Popen(
        ["sleep", "43.219"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(60):
            if _yes_no("which_process_matches 'sleep 43.219'") == "YES":
                break
            time.sleep(0.05)
        else:
            pytest.fail("the spawned sleep process was never visible to ps")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_docker_emit_lines_are_dangling_only_never_a():
    cmds = [
        line.strip()
        for line in SCRIPT.read_text().splitlines()
        if line.strip().startswith('emit "docker')
    ]
    assert cmds == [
        'emit "docker container prune -f"',
        'emit "docker image prune -f"',
        'emit "docker builder prune -f"',
    ]
