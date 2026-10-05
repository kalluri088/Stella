"""The Stella-owned ``llama-server`` brain process and its client.

Research report 09 measured the brain launch line on this machine
(gpt-oss-20b mxfp4, ``-cmoe`` experts, 2 KV slots with q8_0 cache,
ngram-mod speculation: ~2.77 GB VRAM, 8.6 t/s voice turns, +20 % on
structured output), and the systemd-topology report found the real gap:
nothing supervised the brain server. Stella therefore spawns and owns
it — the launch line and the VRAM budget stay in one head, and stopping
Stella stops the server (SIGTERM first, so the process cleans itself
up; terminate/kill only if it ignores that).

The server speaks the OpenAI-compatibility dialect on ``/v1`` — the
same chat-completions-with-tools protocol Ollama's compatibility
endpoint serves — so the client reuses that exact code path. ``--jinja``
is part of the launch line because gpt-oss tool calls run through its
Harmony chat template.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Self

from stella.childproc import guarded_popen
from stella.llm import UsageRecorder
from stella.ollama_client import OllamaLLMClient
from stella.portable import polite_stop

DEFAULT_LLAMA_SERVER_BINARY = "llama-server"
DEFAULT_LLAMA_SERVER_PORT = 8080
DEFAULT_READY_TIMEOUT_SECONDS = 240.0
HEALTH_POLL_SECONDS = 0.5
TERMINATE_GRACE_SECONDS = 10.0
KILL_GRACE_SECONDS = 5.0

# The measured Round C launch line (report 09), with one honest
# amendment: -c is the total KV across slots, so report 09's
# "-c 8192 -np 2" gives each slot 4096 tokens — and Stella's real first
# turn (persona + tool JSON) measured 5299 tokens and was refused with
# HTTP 400. 16384 keeps every slot at the size the prompt needs; extra
# KV at q8_0 costs tens of MiB per 8k tokens (report 09), and the live
# smoke re-verified the total fits alongside the embed model.
# -ngl 99 -cmoe keeps the attention layers on GPU with all MoE expert
# weights in RAM (the only configuration that fits a 6 GB card); -np 2
# keeps voice turns at full speed; ngram-mod speculation is free when
# it cannot help.
BRAIN_LAUNCH_ARGS: tuple[str, ...] = (
    "-ngl",
    "99",
    "-cmoe",
    "-c",
    "16384",
    "-np",
    "2",
    "-ctk",
    "q8_0",
    "-ctv",
    "q8_0",
    "--spec-type",
    "ngram-mod",
    "--spec-ngram-mod-n-min",
    "2",
    "--spec-ngram-mod-n-max",
    "8",
    "--jinja",
)


class LlamaBrainError(RuntimeError):
    """The brain server could not be started or stopped cleanly."""


class LlamaBrainServer:
    """One ``llama-server`` child process with Stella owning its life.

    :meth:`start` launches the measured command and waits for the
    health endpoint to say the model is loaded; :meth:`stop` runs the
    same SIGINT → terminate → kill ladder the voice player uses, so a
    Stella shutdown never leaves a 13 GB model resident.
    """

    def __init__(
        self,
        model_path: str,
        *,
        binary: str = DEFAULT_LLAMA_SERVER_BINARY,
        host: str = "127.0.0.1",
        port: int = DEFAULT_LLAMA_SERVER_PORT,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self.model_path = model_path
        self.binary = binary
        self.host = host
        self.port = port
        self.ready_timeout = ready_timeout
        self._extra_args = tuple(extra_args)
        self._process: subprocess.Popen[bytes] | None = None
        self._log_path: str | None = None
        self._lock = threading.Lock()

    @property
    def command(self) -> list[str]:
        return [
            self.binary,
            "-m",
            self.model_path,
            "--host",
            self.host,
            "--port",
            str(self.port),
            *BRAIN_LAUNCH_ARGS,
            *self._extra_args,
        ]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def alive(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def start(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            if self._health_ok():
                # Someone else already answers on this port: waiting for
                # our own bind failure could otherwise read their health
                # as our readiness and quietly use the wrong server.
                raise LlamaBrainError(
                    f"Port {self.port} is already serving a healthy "
                    "llama.cpp server. Stop that server or set "
                    "STELLA_LLAMA_SERVER_PORT to a free port."
                )
            log_handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="wb",
                prefix=f"stella-brain-{os.getpid()}-",
                suffix=".log",
                delete=False,
            )
            self._log_path = log_handle.name
            try:
                process = guarded_popen(
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            except OSError as error:
                log_handle.close()
                os.unlink(self._log_path)
                self._log_path = None
                raise LlamaBrainError(
                    f"Stella could not start the brain server ({error}). "
                    "Is llama-server installed, or set "
                    "STELLA_LLAMA_SERVER_BINARY to its path?"
                ) from error
            finally:
                log_handle.close()
            self._process = process
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = self._log_tail()
                self.stop()
                raise LlamaBrainError(
                    "The brain server exited before it was ready "
                    f"(code {process.returncode}). {detail}"
                )
            if self._health_ok():
                return
            time.sleep(HEALTH_POLL_SECONDS)
        detail = self._log_tail()
        self.stop()
        raise LlamaBrainError(
            f"The brain server did not become ready within "
            f"{self.ready_timeout:.0f} s. {detail}"
        )

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            log_path, self._log_path = self._log_path, None
        if process is not None and process.poll() is None:
            # SIGINT is llama.cpp's own clean-shutdown path (the same
            # ladder the audio player uses); escalate only if ignored.
            # Windows has no way to ask a child for one, so there the
            # polite rung is terminate() and the ladder below simply
            # finds the server already gone.
            try:
                polite_stop(process)
            except OSError:  # pragma: no cover - process just died
                pass
            try:
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()
                    process.wait(timeout=KILL_GRACE_SECONDS)
        if log_path is not None:
            try:
                os.unlink(log_path)
            except OSError:  # pragma: no cover - nothing left to clean
                pass

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _health_ok(self) -> bool:
        url = f"http://{self.host}:{self.port}/health"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            # Connection refused while the socket is still binding is
            # the normal first seconds of a model load, not a failure.
            return False

    def _log_tail(self) -> str:
        if self._log_path is None:
            return ""
        try:
            with open(self._log_path, "rb") as handle:
                handle.seek(max(0, os.path.getsize(self._log_path) - 2000))
                tail = handle.read().decode("utf-8", "replace").strip()
        except OSError:  # pragma: no cover - log is best-effort
            return ""
        if not tail:
            return ""
        return "Last server output: " + tail.splitlines()[-1]


class LlamaServerLLMClient(OllamaLLMClient):
    """Chat client for a Stella-owned llama-server.

    llama-server's ``/v1`` endpoint serves the same OpenAI-compatible
    chat-completions-with-tools protocol as Ollama's compatibility
    endpoint (and, like it, no Responses API), so this is exactly the
    ``native=False`` path of :class:`OllamaLLMClient` under a local
    placeholder key; the answer guard in the Brain stays the trusted
    enforcement point for tool use.
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "stella-local",
        *,
        answer_max_output_tokens: int | None = None,
        decision_max_output_tokens: int | None = None,
        usage: UsageRecorder | None = None,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=api_key,
            native=False,
            answer_max_output_tokens=answer_max_output_tokens,
            decision_max_output_tokens=decision_max_output_tokens,
            usage=usage,
        )
