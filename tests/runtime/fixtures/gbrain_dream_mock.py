#!/usr/bin/env python3
"""Synthetic credential-free Anthropic-compatible mock for the issue #126
gbrain dream interruption/retry conformance gate.

Serves the Anthropic Messages API over loopback (127.0.0.1 only):

  - ``POST /v1/messages`` — dispatches on the request's ``model`` field:
      * the triage model returns an immediate deterministic high-score
        triage verdict (``{"score": 0.9, ...}``, above the default
        ``dream.triage.threshold`` of 0.5);
      * the synthesis model returns a deterministic one-page synthesis
        JSON, but the FIRST synthesis request waits ``DELAY_MS`` before
        answering — that wait is the gate's kill window: the test SIGKILLs
        the dream parent while the inline-drained subagent child is
        claimed and in flight.
  - ``GET /v1/models`` — health probe surface.

The FIRST synthesis request also writes ``MARKER`` so the test can kill
the parent exactly after child claim. Every request is appended as one
JSON line to ``LOG`` (path/phase/model/seq) for evidence.

Binds 127.0.0.1 only; no credentials; no external network; stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def _envelope(model: str, text: str) -> bytes:
    """A minimal valid Anthropic Messages API response carrying ``text`` as
    the sole assistant text block with a clean ``end_turn`` stop."""
    payload = {
        "id": "msg_dream_recovery_mock",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    return json.dumps(payload).encode("utf-8")


def main() -> int:
    if len(sys.argv) != 8:
        print(
            "usage: gbrain_dream_mock.py PORT LOG PID TRIAGE_JSON PAGE_JSON "
            "DELAY_MS MARKER",
            file=sys.stderr,
        )
        return 2
    port = int(sys.argv[1])
    log_path = sys.argv[2]
    pid_path = sys.argv[3]
    triage_json_path = sys.argv[4]
    page_json_path = sys.argv[5]
    delay_ms = int(sys.argv[6])
    marker_path = sys.argv[7]

    with open(triage_json_path, encoding="utf-8") as fh:
        triage_text = fh.read()
    with open(page_json_path, encoding="utf-8") as fh:
        page_text = fh.read()

    state = {"seq": 0, "synth_calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def _log(self, phase: str, model: str, body: str = "") -> None:
            state["seq"] += 1
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "path": self.path,
                                "phase": phase,
                                "model": model,
                                "seq": state["seq"],
                                "body_len": len(body),
                            }
                        )
                        + "\n"
                    )
            except OSError:
                pass

        def _respond(self, payload: bytes) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass  # client gone (e.g. parent SIGKILLed mid-delay)

        def do_GET(self):
            self._log("models", "")
            self._respond(
                json.dumps(
                    {"data": [{"id": "conformance-mock", "object": "model"}]}
                ).encode("utf-8")
            )

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            try:
                request = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                request = {}
            model = str(request.get("model", "") or "")
            if "triage" in model:
                self._log("triage", model, body.decode("utf-8", "replace"))
                self._respond(_envelope(model, triage_text))
                return
            self._log("synthesis", model, body.decode("utf-8", "replace"))
            state["synth_calls"] += 1
            if state["synth_calls"] == 1:
                try:
                    with open(marker_path, "w", encoding="utf-8") as fh:
                        fh.write("first-synthesis-request\n")
                except OSError:
                    pass
                time.sleep(delay_ms / 1000.0)
            self._respond(_envelope(model, page_text))

        def log_message(self, format, *args):  # noqa: A002 - base-class signature
            pass

    with open(pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
