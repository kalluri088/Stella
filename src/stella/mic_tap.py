"""One microphone capture, several in-process consumers.

Push-to-talk, the wake ear and the wake utterance watcher all want
frames from the same device, and two of them genuinely overlap: a woken
capture records the utterance while its own watcher endpoints it. Before
this module each of them brought its own ``pw-record`` child, which is
at best a duplicated device open and at worst the busy microphone every
unexplained voice failure gets blamed on.

The tap owns exactly one capture subprocess and hands each subscriber
whole frames from it. It knows nothing about what the frames mean:
detection, endpointing and recording decisions stay with the consumers
that already make them.

Two rules keep this honest under load:

* **A slow consumer loses frames, never the microphone.** Each
  subscriber has a bounded queue and a full queue drops its oldest
  frame, so one wedged consumer cannot stall the others or the reader.
* **Reader death is one message, then silence.** If the capture ends
  while subscribers are live, the tap marks itself failed and every
  ``poll()`` returns ``None`` from then on. Each consumer already
  retires on ``None`` exactly as it did on EOF from its own pipe, so a
  dead microphone disables the ears instead of spamming them.
"""

from __future__ import annotations

import queue
import subprocess
import threading
from collections.abc import Sequence

from stella.childproc import guarded_popen
from stella.voice import VoiceError, _cancel_process_tree

__all__ = ["FRAME_BYTES", "MicTap", "TapClient", "read_frame"]

#: One 32 ms frame of 16 kHz mono s16le: the unit every consumer thinks
#: in (``barge_in.FRAME_BYTES`` and ``wake.FRAME_BYTES`` are this value).
FRAME_BYTES = 1024

#: How far a subscriber may fall behind before its oldest frame is
#: dropped: ~250 ms of audio, well past any real consumer's jitter.
DEFAULT_BACKLOG = 8


def read_frame(stream, frame_bytes: int = FRAME_BYTES) -> bytes | None:
    """One whole frame from ``stream``, or ``None`` at EOF or on error.

    The single copy of the short-read loop the three listeners each
    carried: a capture that hands over a partial frame is not the end of
    the world, but a read that returns nothing is.
    """

    data = b""
    while len(data) < frame_bytes:
        try:
            part = stream.read(frame_bytes - len(data))
        except OSError:
            return None
        if not part:
            return None  # EOF
        data += part
    return data


class TapClient:
    """One consumer's view of the shared capture.

    ``poll()`` waits briefly for the next frame and returns ``None``
    permanently once the tap is stopped, has failed, or this client has
    been closed — the same "nothing more will come" that EOF meant to a
    subprocess listener.
    """

    def __init__(self, name: str, tap: MicTap, backlog: int) -> None:
        self.name = name
        self._tap = tap
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=backlog)
        self._closed = threading.Event()
        self._pending = b""

    def poll(self) -> bytes | None:
        while not self._closed.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            return None if item is None else item
        return None

    def read(self, size: int = -1) -> bytes:
        """Present this subscriber as a readable stream.

        The listeners already read whole frames from a capture pipe, so a
        subscriber that answers ``read`` the same way — one frame at a
        time, ``b""`` at the end — slots in without a second loop to
        maintain. ``size`` below a frame length is served from a buffer
        rather than by splitting a frame across reads.
        """

        if size is None or size < 0:
            size = FRAME_BYTES
        if not self._pending:
            frame = self.poll()
            if frame is None:
                return b""  # EOF: stopped, failed, or closed
            self._pending = frame
        if size >= len(self._pending):
            data, self._pending = self._pending, b""
            return data
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def close(self) -> None:
        """Leave the tap: no more frames here, and maybe end the capture."""

        if self._closed.is_set():
            return
        self._terminate()
        self._tap._leave(self)

    def _terminate(self) -> None:
        self._closed.set()
        self._pending = b""  # a half-served frame dies with the subscription
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put_nowait(None)

    def _offer(self, frame: bytes) -> None:
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(frame)
            return
        except queue.Full:
            pass
        # A consumer this far behind is not helped by an older frame.
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            pass


class MicTap:
    """The one capture subprocess for this site, shared by its consumers."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        frame_bytes: int = FRAME_BYTES,
    ) -> None:
        if command is None:
            # Only the default reaches for barge_in's argv builder; a
            # caller that passes a command keeps this module independent.
            from stella.barge_in import capture_command

            command = capture_command(None)
        self._command = list(command)
        self._frame_bytes = frame_bytes
        self._clients: list[TapClient] = []
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        #: True while a deliberate teardown is underway, so the reader's
        #: EOF is recognised as "we asked for that" and not a fault.
        self._closing = False
        self.failed = False
        self.error: str | None = None

    def subscribe(self, name: str, *, backlog: int = DEFAULT_BACKLOG) -> TapClient:
        """Join the capture, opening it for the first subscriber."""

        client = TapClient(name, self, backlog)
        with self._lock:
            if self.failed:
                raise VoiceError(
                    self.error or "The microphone is not available to Stella."
                )
            self._clients.append(client)
            if self._thread is None:
                self._closing = False
                self._open_locked()
        return client

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """End the capture; every subscriber learns it is over."""

        with self._lock:
            self._closing = True
            process, thread = self._process, self._thread
            self._process = self._thread = None
            clients, self._clients = list(self._clients), []
        for client in clients:
            client._terminate()
        self._close_capture(process, thread)

    def _leave(self, client: TapClient) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
            if self._clients or self._thread is None:
                return
            # The last subscriber out turns the microphone off.
            self._closing = True
            process, thread = self._process, self._thread
            self._process = self._thread = None
        self._close_capture(process, thread)

    def _close_capture(
        self,
        process: subprocess.Popen[bytes] | None,
        thread: threading.Thread | None,
    ) -> None:
        if process is not None and process.poll() is None:
            _cancel_process_tree(process)
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2)

    def _open_locked(self) -> None:
        try:
            process = guarded_popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.failed = True
            self.error = (
                f"Stella could not open the microphone ({error}). Voice "
                "features are disabled until a capture can start."
            )
            raise VoiceError(self.error) from error
        self._process = process
        self._thread = threading.Thread(
            target=self._run,
            args=(process.stdout, process),
            name="stella-mic-tap",
            daemon=True,
        )
        self._thread.start()

    def _run(self, stream, process: subprocess.Popen[bytes]) -> None:
        ended = False
        try:
            if stream is None:  # pragma: no cover - Popen always gives a pipe
                ended = True
                return
            while True:
                frame = read_frame(stream, self._frame_bytes)
                if frame is None:
                    ended = True
                    return
                with self._lock:
                    clients = list(self._clients)
                for client in clients:
                    client._offer(frame)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError as error:
                    del error
            if process.poll() is None:
                _cancel_process_tree(process)
            with self._lock:
                self._process = self._thread = None
                # A capture that ended by itself while consumers were
                # still subscribed is a fault; being turned off is not.
                fault = ended and not self._closing and bool(self._clients)
                clients = list(self._clients) if fault else []
                if fault:
                    self.failed = True
                    self.error = (
                        "The microphone capture ended. The ears are off "
                        "until Stella can open it again."
                    )
                    self._clients.clear()
            for client in clients:
                client._terminate()
