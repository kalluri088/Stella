"""Subprocess Tier-1 judge: talks to ``laya_runner.py`` in the laya venv.

laya is not (and must not become) a Stella dependency — it drags torch
and a GPU-resident 2.5 GiB footprint with it. This client spawns the
standalone runner under the venv's interpreter and speaks line-delimited
JSON, so the core project stays stdlib-only while the fuzzy tier reuses
the measured resident-process cost model (~40 ms per event batch).

Failure is always :class:`TierOneUnavailable` plus a dead client: a
judge that hung, crashed or never loaded is dropped (and respawned on
the next question) rather than trusted with stale answers — following
the voice-degradation precedent, routing dies, nothing else.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

from stella.childproc import guarded_popen
from stella.event_bus import Event, TierOneUnavailable

RUNNER_PATH = Path(__file__).resolve().parent / "laya_runner.py"
DEFAULT_READY_TIMEOUT_SECONDS = 300.0
DEFAULT_ASK_TIMEOUT_SECONDS = 60.0


class LayaJudge:
    """One resident ``laya_runner.py`` answering batched questions."""

    def __init__(
        self,
        python: str,
        *,
        runner_path: Path = RUNNER_PATH,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        ask_timeout: float = DEFAULT_ASK_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._python = python
        self._runner_path = runner_path
        self._ready_timeout = ready_timeout
        self._ask_timeout = ask_timeout
        self._env = dict(env) if env is not None else None
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._counter = 0
        self._lock = threading.Lock()

    def ask(
        self,
        event: Event,
        questions: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, Mapping[str, object]]:
        """One request for all questions; never returns a partial lie."""

        with self._lock:
            self._ensure_running_locked()
            assert self._process is not None and self._process.stdin is not None
            self._counter += 1
            request_id = self._counter
            request = {
                "id": request_id,
                "state": event.as_text(),
                "questions": dict(questions),
            }
            try:
                self._process.stdin.write(json.dumps(request) + "\n")
                self._process.stdin.flush()
                reply = self._read_reply_locked(request_id)
            except (OSError, ValueError) as error:
                self._kill_locked()
                raise TierOneUnavailable(
                    f"the laya judge could not be reached ({error})"
                ) from error
            if reply is None:
                self._kill_locked()
                raise TierOneUnavailable(
                    "the laya judge did not answer within "
                    f"{self._ask_timeout:.0f} s; it was dropped and will "
                    "be restarted on the next question"
                )
        if not reply.get("ok"):
            raise TierOneUnavailable(
                f"the laya judge refused the question: "
                f"{reply.get('error', 'unknown error')}"
            )
        answers = reply.get("answers")
        if not isinstance(answers, Mapping):
            raise TierOneUnavailable("the laya judge returned no answers")
        return answers

    def close(self) -> None:
        with self._lock:
            self._kill_locked()

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ----------------------------------------------------------- internals

    def _ensure_running_locked(self) -> None:
        if self._process is not None:
            return
        try:
            process = guarded_popen(
                [self._python, str(self._runner_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=self._env,
            )
        except OSError as error:
            raise TierOneUnavailable(
                f"Stella could not start the laya judge interpreter "
                f"({error})"
            ) from error
        assert process.stdout is not None
        self._process = process
        self._lines = queue.Queue()
        reader = threading.Thread(
            target=self._pump_stdout,
            args=(process.stdout,),
            name="stella-laya-reader",
            daemon=True,
        )
        reader.start()
        ready = self._await_line_locked(self._ready_timeout)
        if ready is None:
            self._kill_locked()
            raise TierOneUnavailable(
                f"the laya judge did not load the model within "
                f"{self._ready_timeout:.0f} s"
            )
        try:
            payload = json.loads(ready)
        except json.JSONDecodeError:
            self._kill_locked()
            raise TierOneUnavailable(
                "the laya judge sent a response Stella could not read"
            ) from None
        if not payload.get("ready"):
            self._kill_locked()
            raise TierOneUnavailable(
                f"the laya judge could not load laya: "
                f"{payload.get('error', 'unknown error')}"
            )

    def _pump_stdout(self, stream) -> None:
        for line in stream:
            self._lines.put(line)
        self._lines.put(None)

    def _read_reply_locked(self, request_id: int) -> dict | None:
        """Next good reply for this id; stale/odd lines are skipped.

        A timeout kills the whole process (via the caller), because a
        late reply would desynchronize every future request.
        """

        while True:
            line = self._await_line_locked(self._ask_timeout)
            if line is None:
                return None
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == request_id:
                return payload
            # A reply for an abandoned request: drop it and keep waiting.

    def _await_line_locked(self, timeout: float) -> str | None:
        """Next runner line, or None on timeout or EOF (both are fatal).

        A timed-out line can still arrive later, but the caller kills
        the whole process on timeout — a late reply would desynchronize
        every future request, so partial reads are never trusted.
        """

        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def _kill_locked(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
                process.wait(timeout=5)
