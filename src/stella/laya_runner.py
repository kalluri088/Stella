"""Standalone Tier-1 judge runner for the event bus (layav venv only).

This file is deliberately import-free of ``stella``: it is executed by
the *laya virtualenv's* interpreter (``~/tools/layav/bin/python``),
which has laya but not Stella. Stella talks to it over line-delimited
JSON on stdin/stdout — the same no-dependency subprocess pattern the
command transcription and speech providers use.

Protocol: on start the runner loads the GPU-resident agent and answers
``{"ready": true}`` (or ``{"ready": false, "error": ...}`` and exits).
Each request line ``{"id": n, "state": str, "questions": {...}}`` gets
exactly one reply line ``{"id": n, "ok": true, "answers": {...}}``.
EOF ends the session.

The preset-shape rule (report 12) lives in Stella's own validator; this
runner passes questions to laya unchanged so laya's errors surface
verbatim in the ``error`` field.
"""

import json
import os
import sys


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> int:
    try:
        import laya
    except ImportError as error:  # pragma: no cover - venv-dependent
        _emit({"ready": False, "error": f"laya is not importable: {error}"})
        return 1
    try:
        device = os.environ.get("LAYA_DEVICE", "cuda")
        agent = laya.load(device=device)
    except Exception as error:  # noqa: BLE001 - reported, never traced out
        _emit({"ready": False, "error": f"{type(error).__name__}: {error}"})
        return 1
    _emit({"ready": True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            answers = agent.predict(request["state"], request["questions"])
            payload = {
                "id": request_id,
                "ok": True,
                "answers": answers.get("answers", answers),
            }
        except Exception as error:  # noqa: BLE001 - stay serving next line
            payload = {
                "id": request_id,
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }
        _emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
