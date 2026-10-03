"""Local OCR for the desktop seam.

tesseract is cross-platform and compositor-independent — it needs no
display server, no session marker and no protocol, so one recognizer
serves every adapter. The measured reason it exists (report 11): a grim
capture (~70 ms window) piped straight into tesseract (~1.4 s) answers
"what does this window say?" with zero cloud and zero GPU.
"""

from __future__ import annotations

from stella.desktop.capabilities import (
    OCR_SCREEN,
    OCR_WINDOW,
    RecognizeUnavailable,
)
from stella.desktop.runner import Runner, error_detail, subprocess_runner

OCR_TIMEOUT_SECONDS = 30.0

# Report 11, measured on this machine: a window is one uniform text block,
# so --psm 6; a whole desktop is sparse and scattered and needs --psm 11
# (and costs about 9 s). PSM is a tesseract argument, so it lives here and
# nowhere above the adapter — the tools only pass the hint.
_PSM_BY_HINT = {OCR_WINDOW: "6", OCR_SCREEN: "11"}


class TesseractRecognizer:
    """Implements :class:`stella.desktop.capabilities.Recognizer`."""

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        binary: str = "tesseract",
        timeout: float = OCR_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner
        self.binary = binary
        self._timeout = timeout

    def required_binaries(self) -> tuple[str, ...]:
        return (self.binary,)

    def recognize(self, png: bytes, hint: str) -> str:
        """OCR one PNG; empty text is an honest answer, not a failure."""

        psm = _PSM_BY_HINT.get(hint, _PSM_BY_HINT[OCR_SCREEN])
        # The trailing ``-`` for the output name is what makes tesseract
        # write text to stdout instead of guessing a file; the input ``-``
        # reads the picture from stdin, so no pixels ever hit the disk.
        argv = [self.binary, "-", "stdout", "--psm", psm]
        try:
            result = self._runner(argv, timeout=self._timeout, stdin=png)
        except OSError as failure:
            raise RecognizeUnavailable(
                f"Local OCR failed ({self.binary} could not run: {failure})."
            ) from failure
        if result.returncode != 0:
            raise RecognizeUnavailable(
                "Local OCR failed. "
                + error_detail(result, label="tesseract said")
            )
        return result.stdout.decode("utf-8", errors="replace").strip()


__all__ = ["OCR_TIMEOUT_SECONDS", "TesseractRecognizer"]
