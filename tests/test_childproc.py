"""The parent-death guarantees and the orphan sweep (report 34's fix).

The tests that watch a real kernel do its work (PR_SET_PDEATHSIG,
``/proc``) are marked for the platform that actually has them. The
*decisions* — which guarantee a platform gets, how a child's exit status
is read, when the watchdog fires, whether a preexec_fn is ever handed to
Windows — are fakes, so they run everywhere and cannot be quietly
skipped away.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from stella import childproc
from stella.childproc import (
    _START_TS,
    CHILD_MARK_ENV,
    WINDOWS,
    _mentions_stella_temp,
    exit_status_finalized,
    guarded_popen,
    parent_death_guarantee,
    recording_finalized_ok,
    start_parent_watchdog,
    sweep_orphaned_children,
    terminate_own_process_group,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="the signalling model is POSIX's"
)
LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="PR_SET_PDEATHSIG and /proc are Linux kernel features",
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
    """End a child a test left running, however this platform kills.

    Cleanup, not the subject: ``SIGKILL`` is the POSIX name for "die now"
    and Windows has none, where any other number through ``os.kill``
    terminates the process outright. Deciding here keeps every test that
    reaps a child runnable everywhere instead of only proving itself on
    the machine that happens to be running it.
    """

    force = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
    try:
        os.kill(pid, force)
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


@LINUX_ONLY
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


@LINUX_ONLY
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


@LINUX_ONLY
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


# ---------------------------------------------------------------------------
# The decisions. These run on every platform because the platform is an
# argument, not an aspiration: they prove which guarantee is chosen and
# what is done with it, without pretending this host is another OS.
# ---------------------------------------------------------------------------


def test_each_platform_is_given_the_guarantee_it_actually_has():
    assert parent_death_guarantee("linux") == "prctl+watchdog"
    assert parent_death_guarantee("win32") == "job+watchdog"
    # macOS has no prctl and no equivalent: it gets the portable half and
    # nothing is armed at spawn time. Saying so is the point.
    assert parent_death_guarantee("darwin") == "watchdog"
    assert parent_death_guarantee("freebsd14") == "watchdog"


def _spawn_for(monkeypatch, sys_platform):
    """Run ``guarded_popen`` against a stand-in ``Popen`` and report it.

    Returns the fake process, the keyword arguments it was handed, and
    the spawn calls that reached the Windows Job Object arm.
    """

    calls: list[tuple[list[str], dict]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((list(argv), kwargs))
            self.args = list(argv)
            self.returncode = 0
            self.pid = os.getpid()
            # What CPython's Windows Popen stores: the hProcess that
            # CreateProcessW returned, and the only handle available here.
            self._handle = 1234

    armed: list[object] = []
    monkeypatch.setattr(childproc.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        childproc,
        "parent_death_guarantee",
        lambda: parent_death_guarantee(sys_platform),
    )
    monkeypatch.setattr(
        childproc, "_arm_windows_job", lambda process: armed.append(process)
    )
    process = guarded_popen(["some", "helper"], stdin=-3)
    assert calls, "guarded_popen did not spawn anything"
    return process, calls[0][1], armed


def test_windows_spawn_never_receives_a_preexec_fn(monkeypatch):
    # subprocess raises ValueError on Windows if one is passed: handing it
    # over would turn every guarded child into a spawn failure.
    _, kwargs, armed = _spawn_for(monkeypatch, "win32")
    assert "preexec_fn" not in kwargs
    assert len(armed) == 1, "the Job Object guarantee was not armed"


def test_linux_spawn_keeps_the_prctl_preexec_fn_unchanged(monkeypatch):
    _, kwargs, armed = _spawn_for(monkeypatch, "linux")
    assert kwargs["preexec_fn"] is childproc._die_with_parent
    assert armed == [], "Linux must not be routed through the Windows job"


def test_macos_spawn_arms_nothing_it_does_not_have(monkeypatch):
    _, kwargs, armed = _spawn_for(monkeypatch, "darwin")
    assert "preexec_fn" not in kwargs
    assert armed == []


def test_every_spawn_carries_the_parent_mark(monkeypatch):
    _, kwargs, _ = _spawn_for(monkeypatch, "win32")
    assert kwargs["env"][CHILD_MARK_ENV] == str(os.getpid())


def test_prctl_is_resolved_lazily_and_never_raises_on_import():
    # The module import must be safe everywhere: CDLL(None) raises
    # ValueError on Windows, not OSError, and WinDLL has no meaning here.
    assert childproc._PRCTL_RESOLVED is False or callable(childproc._PRCTL)
    assert childproc._prctl_function() is None or callable(childproc._prctl_function())
    assert childproc._kernel32() is None or callable(childproc._kernel32)


def test_exit_status_is_read_with_this_platforms_conventions():
    # POSIX: unchanged, including the negative "killed by SIGINT" the
    # kernel reports and the 2 that shell-level recorders produce.
    assert exit_status_finalized(0, sys_platform="linux") is True
    assert exit_status_finalized(-signal.SIGINT, sys_platform="linux") is True
    assert exit_status_finalized(2, sys_platform="linux") is True
    assert exit_status_finalized(1, sys_platform="linux") is False
    assert exit_status_finalized(None, sys_platform="linux") is False
    # Windows has no negative signal codes; a bare 2 is not a signal
    # there, so it must not be read as one.
    assert exit_status_finalized(childproc.WINDOWS_INTERRUPT_EXIT, sys_platform="win32") is True
    assert exit_status_finalized(2, sys_platform="win32") is False
    assert exit_status_finalized(-signal.SIGINT, sys_platform="win32") is False
    # macOS uses the same conventions as the other POSIX systems.
    assert exit_status_finalized(-signal.SIGINT, sys_platform="darwin") is True


def test_recording_finalized_ok_fails_closed_without_a_file(tmp_path):
    path = tmp_path / "capture.wav"
    # A fine status with no file is not a success.
    assert recording_finalized_ok(0, path) is False
    path.write_bytes(b"x" * 44)  # bare RIFF/WAVE header: nothing captured
    assert recording_finalized_ok(0, path) is False
    path.write_bytes(b"x" * 45)
    assert recording_finalized_ok(0, path) is True
    # Death by the interrupt Stella sent is how POSIX reports it, and a
    # Windows console interrupt is the unsigned code — each stated for the
    # platform that produces it, so this pins the file check on every host
    # instead of borrowing the one running the suite.
    assert recording_finalized_ok(-signal.SIGINT, path, sys_platform="linux") is True
    assert (
        recording_finalized_ok(
            childproc.WINDOWS_INTERRUPT_EXIT, path, sys_platform="win32"
        )
        is True
    )
    assert recording_finalized_ok(1, path) is False


@POSIX_ONLY
def test_watchdog_takes_the_child_down_when_its_parent_is_killed():
    """The portable guarantee, end to end, with no prctl involved.

    The helper arms only the watchdog. Killing its parent reparents it,
    which is the event the thread is built to notice on macOS and
    Windows; it must therefore die on its own, in its own session so the
    kill cannot reach anything it does not own.
    """

    helper = (
        "import os, sys, time;"
        "from stella.childproc import start_parent_watchdog;"
        "start_parent_watchdog();"
        "print(os.getpid(), flush=True); time.sleep(30)"
    )
    wrapper = (
        "import subprocess, sys, time;"
        "child = subprocess.Popen([sys.executable, '-c', "
        + repr(helper)
        + "], start_new_session=True); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", wrapper], stdout=subprocess.PIPE, text=True
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    try:
        time.sleep(0.4)  # the watchdog thread is running by now
        parent.kill()
        parent.wait()
        assert _wait_for_death(child_pid, timeout=5.0), (
            "the watchdog did not fire: the portable guarantee is broken"
        )
    finally:
        _reap(child_pid)


def test_watchdog_fires_when_the_tracked_parent_pid_is_gone():
    deaths = []
    thread = start_parent_watchdog(
        4242,
        interval_seconds=0.01,
        alive_fn=lambda pid: False,
        getppid_fn=lambda: 1,
        terminate_fn=lambda: deaths.append(4242),
    )
    thread.join(timeout=2.0)
    assert deaths == [4242], "a dead parent should end the child"


def test_watchdog_fires_on_reparenting_even_if_the_pid_still_exists():
    # On macOS and Windows a child is re-parented when its parent dies;
    # the original pid may meanwhile be reused by something unrelated.
    seen = [4242]
    pids = iter([4242, 1, 1, 1])
    deaths = []
    thread = start_parent_watchdog(
        4242,
        interval_seconds=0.01,
        alive_fn=lambda pid: pid in seen,
        getppid_fn=lambda: next(pids),
        terminate_fn=lambda: deaths.append("reparented"),
    )
    thread.join(timeout=2.0)
    assert deaths == ["reparented"]


def test_watchdog_stays_quiet_while_the_parent_lives():
    alive = {"yes": True}
    terminations = []
    thread = start_parent_watchdog(
        4242,
        interval_seconds=0.01,
        alive_fn=lambda pid: alive["yes"],
        getppid_fn=lambda: 4242,
        terminate_fn=lambda: terminations.append("fired"),
    )
    try:
        time.sleep(0.1)
        assert thread.is_alive(), "the watchdog must outlive a live parent"
        assert terminations == [], "a live parent must not end the child"
    finally:
        alive["yes"] = False
        thread.join(timeout=2.0)
    assert not thread.is_alive(), "the thread must end once it has acted"


def test_sweep_is_an_explicit_no_op_off_linux(monkeypatch):
    # Not a scan whose OSError handling turns "cannot look" into
    # "nothing was orphaned": off Linux there is nothing to claim.
    called = []
    monkeypatch.setattr(childproc, "platform_name", lambda *a: "darwin")
    monkeypatch.setattr(
        childproc, "_sweep_linux", lambda output_fn: called.append(1) or 99
    )
    assert sweep_orphaned_children(output_fn=lambda _: None) == 0
    assert called == []


def test_sweep_still_runs_on_linux(monkeypatch):
    monkeypatch.setattr(childproc, "platform_name", lambda *a: "linux")
    monkeypatch.setattr(childproc, "_sweep_linux", lambda output_fn: 7)
    assert sweep_orphaned_children(output_fn=lambda _: None) == 7


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("/tmp/stella-voice-9-abc/capture.wav", True),
        ("--out=/tmp/stella-voice-9-abc/capture.wav", True),
        (
            "C:\\Users\\me\\AppData\\Local\\Temp\\stella-voice-9-abc\\capture.wav",
            True,
        ),
        ("stella-voice-9-abc/capture.wav", True),
        ("/tmp/somebody-elses-stella-voice-9/capture.wav", False),
        ("/tmp/unrelated/capture.wav", False),
        ("", False),
    ],
)
def test_temp_path_matching_does_not_assume_a_separator(token, expected):
    assert _mentions_stella_temp(token) is expected


@pytest.mark.parametrize(
    "failure",
    [
        # What Windows actually does: LoadLibrary wants a name, not the
        # main program, so the call is rejected before anything loads.
        TypeError("LoadLibrary() argument 1 must be str, not None"),
        # What a restricted dynamic loader does instead. The test used to
        # call this one "the Windows failure", which is how the product
        # ended up catching the wrong exception.
        ValueError("nothing to load"),
    ],
    ids=["windows-rejects-unnamed-load", "loader-restricted"],
)
def test_prctl_lookup_survives_a_cdll_that_cannot_load(failure, monkeypatch):
    # Resolving prctl must never raise, because a failed lookup is a
    # platform fact, not a spawn error.
    def boom(*args, **kwargs):
        raise failure

    monkeypatch.setattr(childproc, "_PRCTL_RESOLVED", False)
    monkeypatch.setattr(childproc, "_PRCTL", None)
    monkeypatch.setattr(childproc.ctypes, "CDLL", boom)
    assert childproc._prctl_function() is None
    assert childproc._die_with_parent() is None


def test_prctl_lookup_survives_a_missing_symbol(monkeypatch):
    class NoPrctl:
        pass

    monkeypatch.setattr(childproc, "_PRCTL_RESOLVED", False)
    monkeypatch.setattr(childproc, "_PRCTL", None)
    monkeypatch.setattr(childproc.ctypes, "CDLL", lambda *a, **k: NoPrctl())
    assert childproc._prctl_function() is None


def test_kernel32_lookup_is_absent_not_fatal_off_windows():
    # WinDLL does not exist in CPython on Linux: the lookup has to answer
    # "no" rather than raise, or importing the guarantee layer would break
    # the platform it is supposed to protect.
    assert childproc._kernel32() is None or callable(childproc._kernel32)


def test_arm_windows_job_is_quiet_when_there_is_no_kernel32():
    class Process:
        _handle = 1234

    # On this host there is no kernel32, so the arm must be a no-op rather
    # than an exception escaping guarded_popen.
    assert childproc._arm_windows_job(Process()) is None


# ---------------------------------------------------------------------------
# The hazard review's claims, as tests. Each one states what a wrong
# answer would do to a live machine, because these are the code paths
# that signal processes the caller does not personally own.
# ---------------------------------------------------------------------------


@LINUX_ONLY
def test_sweep_matching_is_confined_to_the_exact_marker(monkeypatch):
    """Only the exact env marker makes a process killable by the sweep.

    A near-miss env name must not match (``startswith`` is over the
    whole ``NAME=`` chunk, not a substring search), and the process
    running the sweep must never be in its own candidate list even when
    it carries a marker with a stale value — that would make a boot
    sweep a suicide (a shell started from a guarded child leaks the
    mark to whatever Stella the user later launches from it).
    """

    lookalike = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "STELLA_CHILD_PIDX": str(_dead_pid())},
        **_DEVNULL,
    )
    marked = _spawn_marked(mark=str(os.getpid()))
    monkeypatch.setenv(CHILD_MARK_ENV, str(_dead_pid()))
    try:
        marks: dict[int, int] = {}
        deadline = time.time() + 5.0
        while marked.pid not in marks and time.time() < deadline:
            marks = dict(childproc._marked_children())
            if marked.pid not in marks:
                time.sleep(0.05)
        assert marks.get(marked.pid) == os.getpid()
        assert lookalike.pid not in marks, "a near-miss env name matched the marker"
        assert os.getpid() not in marks, (
            "the sweep counted the process running it as a killable orphan"
        )
    finally:
        _reap(lookalike.pid)
        _reap(marked.pid)
        lookalike.wait()
        marked.wait()


@LINUX_ONLY
def test_sweep_never_signals_a_process_that_bears_no_marker():
    """Command-line matching alone never kills.

    An unrelated live process whose argv *names* a Stella-shaped temp
    path is visible to the referencing-process scan, but it is only
    ever signalled when a stale orphaned directory it references is
    actually being reclaimed. With no such directory on disk this sweep
    must pass over it entirely — this is the proof that no loose
    prefix/name match against ``/proc/<pid>/cmdline`` reaches a signal.
    """

    decoy = _spawn_marked(
        mark=None,
        extra_argv=["/tmp/stella-voice-999999-pytest-decoy/capture.wav"],
    )
    try:
        sweep_orphaned_children(output_fn=lambda _: None)
        time.sleep(0.3)
        assert decoy.poll() is None, (
            "the sweep signalled a process it does not track"
        )
    finally:
        _reap(decoy.pid)
        decoy.wait()


@POSIX_ONLY
def test_watchdog_in_a_shared_process_group_kills_only_itself():
    """The watchdog's kill cannot take the group — and therefore not
    the user's terminal — down with it.

    Unlike the reparenting test above, this helper does *not* get its
    own session: it shares the process group of the test process
    itself. When its parent dies the watchdog fires inside that shared
    group, and the only safe answer is to end itself. If
    ``terminate_own_process_group`` ever regresses to an unconditional
    ``killpg``, this test SIGKILLs the running test session — which is
    precisely the failure the group-leader guard exists to prevent on
    the user's machine.
    """

    helper = (
        "import os, sys, time;"
        "from stella.childproc import start_parent_watchdog;"
        "start_parent_watchdog();"
        "print(os.getpid(), flush=True); time.sleep(30)"
    )
    wrapper = (
        "import subprocess, sys, time;"
        "child = subprocess.Popen([sys.executable, '-c', "
        + repr(helper)
        + "]); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", wrapper], stdout=subprocess.PIPE, text=True
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    try:
        time.sleep(0.4)  # the watchdog thread is running by now
        parent.kill()
        parent.wait()
        assert _wait_for_death(child_pid, timeout=5.0), (
            "the watchdog did not fire: the portable guarantee is broken"
        )
        # Reaching this line is the assertion: the test process — a
        # member of this very group — is still alive to run it.
        assert parent.poll() is not None
    finally:
        _reap(child_pid)


def test_parent_alive_never_uses_the_posix_zero_probe_off_posix(monkeypatch):
    # os.kill(pid, 0) is a liveness probe on POSIX and a TerminateProcess
    # on Windows: the two must not share an implementation.
    calls = []
    monkeypatch.setattr(childproc, "platform_name", lambda *a: WINDOWS)
    monkeypatch.setattr(childproc, "_kernel32", lambda: None)

    def forbidden(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(os, "kill", forbidden)
    assert childproc._parent_alive(4242) is True
    assert calls == [], "the Windows branch must not signal anything"


def _fake_kernel32(exit_code, found=True):
    """A stand-in whose methods are plain functions, like real _FuncPtrs."""
    events = []

    def open_process(access, inherit, pid):
        events.append(("open", access, pid))
        return 7 if found else None

    def get_exit_code(handle, out):
        out._obj.value = exit_code
        return 1

    def close_handle(handle):
        events.append(("close", handle))
        return 1

    return SimpleNamespace(
        OpenProcess=open_process,
        GetExitCodeProcess=get_exit_code,
        CloseHandle=close_handle,
    ), events


def test_parent_alive_windows_probes_without_signalling(monkeypatch):
    kernel32, events = _fake_kernel32(childproc._PROCESS_STILL_ACTIVE)
    monkeypatch.setattr(childproc, "platform_name", lambda *a: WINDOWS)
    monkeypatch.setattr(childproc, "_kernel32", lambda: kernel32)
    assert childproc._parent_alive(4242) is True
    assert ("open", childproc._PROCESS_QUERY_LIMITED_INFORMATION, 4242) in events


def test_parent_alive_windows_reports_a_finished_process(monkeypatch):
    kernel32, _ = _fake_kernel32(0)
    monkeypatch.setattr(childproc, "platform_name", lambda *a: WINDOWS)
    monkeypatch.setattr(childproc, "_kernel32", lambda: kernel32)
    assert childproc._parent_alive(4242) is False


def test_parent_alive_windows_reports_no_process_found(monkeypatch):
    kernel32, _ = _fake_kernel32(259, found=False)
    monkeypatch.setattr(childproc, "platform_name", lambda *a: WINDOWS)
    monkeypatch.setattr(childproc, "_kernel32", lambda: kernel32)
    assert childproc._parent_alive(4242) is False


@POSIX_ONLY
def test_terminate_own_process_group_leaves_a_non_leader_alone(monkeypatch):
    # A helper that is not the group leader must not SIGKILL the group it
    # happens to share with Stella and the user's terminal.
    killpgs = []
    monkeypatch.setattr(os, "killpg", lambda pg, sig: killpgs.append((pg, sig)))
    monkeypatch.setattr(os, "getpgrp", lambda: 1111)
    monkeypatch.setattr(os, "getpid", lambda: 2222)
    exits = []
    monkeypatch.setattr(os, "_exit", lambda code=0: exits.append(code))
    terminate_own_process_group()
    assert killpgs == []
    assert exits == [1]


@POSIX_ONLY
def test_terminate_own_process_group_takes_its_own_group_down(monkeypatch):
    killpgs = []
    monkeypatch.setattr(os, "killpg", lambda pg, sig: killpgs.append((pg, sig)))
    monkeypatch.setattr(os, "getpgrp", lambda: 2222)
    monkeypatch.setattr(os, "getpid", lambda: 2222)
    monkeypatch.setattr(os, "_exit", lambda code=0: None)
    terminate_own_process_group()
    assert killpgs == [(2222, signal.SIGKILL)]
