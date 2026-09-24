"""Protocol tests for the subprocess Tier-1 judge.

laya itself is not a Stella dependency and never appears here: the
runner contract (line-delimited JSON over stdio) is exercised against a
stub script under the project's own interpreter, so the handshake,
id-matching, timeout-respawn and degradation behaviour are all
hermetic. ``laya_runner.py`` — the only file that imports laya — is
checked as far as compiling it.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from stella.event_bus import Event, TierOneUnavailable
from stella.laya_judge import RUNNER_PATH, LayaJudge

STUB_RUNNER = textwrap.dedent(
    """
    import json, os, sys

    state_path = os.environ["STUB_STATE"]
    def state():
        with open(state_path) as handle:
            return json.load(handle)

    if not state().get("ready", True):
        _emit_error = state().get("error", "stub refused to load")
        print(json.dumps({"ready": False, "error": _emit_error}), flush=True)
        sys.exit(1)
    if state().get("garbage"):
        print("this is not json", flush=True)
    else:
        print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if state().get("die"):
            sys.exit(0)
        request = json.loads(line)
        request_id = request.get("id")
        if state().get("prefix_stale"):
            print(json.dumps({"id": request_id + 100, "ok": True,
                              "answers": {"stale": {"choice": "yes"}}}),
                  flush=True)
        if request_id in state().get("ignore", []):
            continue
        if request_id in state().get("refuse", []):
            print(json.dumps({"id": request_id, "ok": False,
                              "error": "laya could not parse the question"}),
                  flush=True)
            continue
        with open(os.environ["STUB_LOG"], "a") as handle:
            handle.write(json.dumps(request) + "\\n")
        answers = state().get(
            "answers", {"question": {"choice": "yes"}}
        )
        extra = {"id": request_id, "ok": True, "answers": answers}
        for key, value in state().get("extra_fields", {}).items():
            extra[key] = value
        print(json.dumps(extra), flush=True)
    """
)


@pytest.fixture
def stub(tmp_path):
    """Factory handing out a judge wired to the stub runner script.

    Each call writes the given scripted state and returns a fresh
    :class:`LayaJudge`; the state file is shared across respawns so a
    scenario can change behaviour between calls.
    """

    script = tmp_path / "stub_runner.py"
    script.write_text(STUB_RUNNER, encoding="utf-8")
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "requests.log"

    def make(initial: dict, **kwargs):
        state_path.write_text(json.dumps(initial), encoding="utf-8")
        kwargs.setdefault("ready_timeout", 10)
        kwargs.setdefault("ask_timeout", 5)
        environment = dict(os.environ)
        environment["STUB_STATE"] = str(state_path)
        environment["STUB_LOG"] = str(log_path)
        judge = LayaJudge(
            sys.executable, runner_path=script, env=environment, **kwargs
        )
        judge.stub_state = state_path  # type: ignore[attr-defined]
        judge.stub_log = log_path  # type: ignore[attr-defined]
        return judge

    return make


EVENT = Event(source="email", text="the server is down", fields=())


def set_state(judge, **values) -> None:
    judge.stub_state.write_text(json.dumps(values), encoding="utf-8")


def read_requests(judge) -> list[dict]:
    if not judge.stub_log.exists():
        return []
    return [
        json.loads(line)
        for line in judge.stub_log.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ---------------------------------------------------------------- handshake


def test_ready_handshake_answers_a_batched_request(stub):
    judge = stub({"answers": {"urgent": {"choice": "yes", "confidence": 0.8}}})
    try:
        answers = judge.ask(
            EVENT, {"urgent": {"type": "choice", "instructions": "x"}}
        )
    finally:
        judge.close()
    assert answers == {"urgent": {"choice": "yes", "confidence": 0.8}}
    requests = read_requests(judge)
    assert len(requests) == 1
    assert requests[0]["state"] == "email the server is down"
    assert requests[0]["questions"] == {
        "urgent": {"type": "choice", "instructions": "x"}
    }
    assert isinstance(requests[0]["id"], int)


def test_a_refused_load_is_fatal_until_recreated(stub):
    judge = stub({"ready": False, "error": "no GPU found"})
    with pytest.raises(TierOneUnavailable, match="could not load laya: no GPU found"):
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    # The dead client lazily retries: with the state fixed, a new
    # handshake happens on the next ask.
    set_state(judge, ready=True, answers={"q": {"noul": 0.4}})
    try:
        assert judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}}) == {
            "q": {"noul": 0.4}
        }
    finally:
        judge.close()


def test_garbage_on_the_handshake_kills_the_judge(stub):
    judge = stub({"garbage": True})
    with pytest.raises(TierOneUnavailable, match="could not read"):
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})


def test_a_missing_interpreter_is_reported_as_unavailable(stub):
    judge = stub({}, ready_timeout=2)
    judge._python = "/nonexistent/laya-python"
    with pytest.raises(TierOneUnavailable, match="could not start"):
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})


# ------------------------------------------------------------------- asks


def test_a_refused_answer_degrades_without_killing_the_process(stub):
    judge = stub({"refuse": [1], "answers": {"q": {"choice": "yes"}}})
    with pytest.raises(TierOneUnavailable, match="refused the question"):
        judge.ask(EVENT, {"q": {"type": "choice", "instructions": "x"}})
    # The process survived (refusal is laya's answer, not a broken pipe),
    # so the next request is served with id 2.
    set_state(judge, answers={"q": {"choice": "no"}})
    try:
        assert judge.ask(EVENT, {"q": {"type": "choice", "instructions": "x"}}) == {
            "q": {"choice": "no"}
        }
    finally:
        judge.close()


def test_replies_for_abandoned_requests_are_skipped(stub):
    judge = stub({"prefix_stale": True, "answers": {"q": {"choice": "yes"}}})
    try:
        answers = judge.ask(EVENT, {"q": {"type": "choice", "instructions": "x"}})
    finally:
        judge.close()
    assert answers == {"q": {"choice": "yes"}}


def test_an_answer_without_an_answers_mapping_is_unavailable(stub):
    judge = stub({"answers": "not-a-mapping", "extra_fields": {}})
    with pytest.raises(TierOneUnavailable, match="no answers"):
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    judge.close()


def test_a_hung_judge_is_killed_and_a_respawn_recovers(stub):
    judge = stub({"ignore": [1], "answers": {"q": {"noul": 0.2}}}, ask_timeout=1)
    with pytest.raises(TierOneUnavailable, match="did not answer within 1 s"):
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    # The hung child was terminated, and the respawn (fresh counter, so
    # this request is id 1 again) is answered once the script moves on.
    set_state(judge, answers={"q": {"noul": 0.3}})
    try:
        assert judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}}) == {
            "q": {"noul": 0.3}
        }
    finally:
        judge.close()


def test_a_judge_that_exits_mid_stream_degrades_cleanly(stub):
    judge = stub({"answers": {"q": {"noul": 0.2}}})
    judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    set_state(judge, die=True, answers={"q": {"noul": 0.2}})
    # First ask after death: the EOF sentinel reads as no reply.
    with pytest.raises(TierOneUnavailable):
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    judge.close()


def test_close_stops_the_runner_and_is_idempotent(stub):
    judge = stub({"answers": {"q": {"noul": 0.2}}})
    judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    process = judge._process
    assert process is not None and process.poll() is None
    judge.close()
    assert process.poll() is not None
    judge.close()


def test_context_manager_closes_even_after_an_error(stub):
    judge = stub({"ready": False, "error": "boom"})
    with pytest.raises(TierOneUnavailable), judge:
        judge.ask(EVENT, {"q": {"type": "noul", "instructions": "x"}})
    assert judge._process is None


# ------------------------------------------------------------------ runner


def test_the_laya_runner_script_compiles_and_imports_nothing_from_stella():
    import py_compile

    py_compile.compile(str(RUNNER_PATH), doraise=True)
    source = RUNNER_PATH.read_text(encoding="utf-8")
    assert "import stella" not in source
    assert "from stella" not in source


def test_the_runner_serves_the_ready_line_under_the_real_contract(tmp_path):
    """Run the real runner where ``laya`` resolves, if it is installed.

    This stays honest without the dependency: outside the layav
    interpreter the load fails and the runner must say so on exactly one
    ready line — which is the same contract the client handshake tests
    already pin.
    """

    process = subprocess.Popen(
        [sys.executable, str(RUNNER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    payload = json.loads(line)
    # The project interpreter has no laya, so this must be the honest
    # failure line (in the layav smoke this exact read yields ready).
    assert payload["ready"] is False
    assert payload["error"]
    process.wait(timeout=10)
    assert process.returncode == 1
