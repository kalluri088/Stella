"""The PDEATHSIG guarantee and the orphan sweep (report 34's fix)."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from stella.childproc import (
    _START_TS,
    CHILD_MARK_ENV,
    guarded_popen,
    sweep_orphaned_children,
)

_DEVNULL = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
}


def _wait_for_death(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _expect_dead(process: subprocess.Popen, timeout: float = 2.0) -> bool:
    """Poll-and-reap variant of :func:`_wait_for_death` for own children."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **_DEVNULL)
    proc.wait()
    return proc.pid


def test_guarded_child_dies_when_parent_process_is_sigkilled():
    script = (
        "import subprocess, time;"
        "from stella.childproc import guarded_popen;"
        "child = guarded_popen(['sleep', '30'], stdin=-3, stdout=-3, "
        "stderr=-3); print(child.pid, flush=True); time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    try:
        time.sleep(0.3)  # the child has exec'd sleep by now
        parent.kill()
        parent.wait()
        assert _wait_for_death(child_pid), (
            "PDEATHSIG did not fire: the kernel guarantee this module "
            "exists for has broken"
        )
    finally:
        _reap(child_pid)


def test_guarded_popen_marks_and_keeps_caller_env():
    script = (
        "import os;"
        "print(os.environ.get('STELLA_CHILD_PID'), os.environ.get('FAKE'))"
    )
    process = guarded_popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "FAKE": "kept"},
    )
    try:
        marked, kept = process.communicate(timeout=10)[0].split()
        assert kept == "kept"
        assert marked == str(os.getpid())
    finally:
        _reap(process.pid)


def _spawn_marked(mark: str | None, extra_argv=()):
    env = dict(os.environ)
    if mark is not None:
        env[CHILD_MARK_ENV] = mark
    argv = [
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        *extra_argv,
    ]
    return subprocess.Popen(argv, env=env, **_DEVNULL)


def test_sweep_kills_marked_orphans_and_spares_live_children():
    orphan = _spawn_marked(mark=str(_dead_pid()))
    live = _spawn_marked(mark=str(os.getpid()))
    try:
        killed = sweep_orphaned_children(output_fn=lambda _: None)
        assert killed >= 1
        assert _expect_dead(orphan), "orphan survived the sweep"
        time.sleep(0.3)
        assert live.poll() is None, "sweep killed a child of a live parent"
    finally:
        _reap(orphan.pid)
        _reap(live.pid)


def _stale_temp_dir(name: str) -> Path:
    path = Path(tempfile.gettempdir()) / name
    path.mkdir(exist_ok=True)
    (path / "capture.wav").write_bytes(b"x")
    old = _START_TS - 120
    os.utime(path, (old, old))
    os.utime(path / "capture.wav", (old, old))
    return path


def test_sweep_reclaims_stale_dirs_and_protects_lived_ones():
    dead = str(_dead_pid())
    stale = _stale_temp_dir(f"stella-voice-{dead}-pytest-old")
    referenced = _stale_temp_dir(f"stella-voice-{dead}-pytest-referenced")
    owned = _stale_temp_dir(f"stella-voice-{os.getpid()}-pytest-mine")
    legacy = _stale_temp_dir("stella-voice-pytest-legacy")
    watcher = _spawn_marked(
        mark=str(os.getpid()),
        extra_argv=[str(referenced / "capture.wav")],
    )
    legacy_writer = _spawn_marked(
        mark=None, extra_argv=[str(legacy / "capture.wav")]
    )
    try:
        sweep_orphaned_children(output_fn=lambda _: None)
        assert not stale.exists(), "stale dir survived the sweep"
        assert referenced.exists(), (
            "sweep deleted a directory a live child references"
        )
        assert owned.exists(), "sweep deleted a directory this pid owns"
        assert not legacy.exists(), (
            "sweep left an orphaned pre-feature directory behind"
        )
        assert _expect_dead(legacy_writer), (
            "the unmarked writer into a reclaimed dir survived"
        )
    finally:
        _reap(watcher.pid)
        _reap(legacy_writer.pid)
        for path in (stale, referenced, owned, legacy):
            shutil.rmtree(path, ignore_errors=True)
