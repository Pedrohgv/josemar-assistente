from __future__ import annotations

from pathlib import Path
import sys
import unittest

try:  # package context (discovery)
    from ._stub_import import stubbed_app_import
except ImportError:  # direct execution: tests/aux_ml/ is sys.path[0]
    from _stub_import import stubbed_app_import


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))
# Stub optional deps ONLY around the application import so the fake httpx does
# not persist for the whole test process (issue #91). The shared helper fully
# restores sys.modules AND the app package's __dict__ attributes, so neither
# `import app.child` nor `from app import child` can reuse fake-bound objects.
with stubbed_app_import("httpx"):
    from app.llama_router import LlamaRouterClient


class BinaryResponse:
    content = b"bad\xffbody\nwith newline"

    @property
    def text(self) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


class LongResponse:
    content = b""
    text = "x" * 600


class RouterErrorTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = object.__new__(LlamaRouterClient)

    def test_error_text_falls_back_for_binary_response(self) -> None:
        text = self.client._error_text(BinaryResponse())

        self.assertEqual(text, "bad\ufffdbody with newline")

    def test_error_text_truncates_long_responses(self) -> None:
        text = self.client._error_text(LongResponse())

        self.assertEqual(len(text), 503)
        self.assertTrue(text.endswith("..."))


if __name__ == "__main__":
    unittest.main()
