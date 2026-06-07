from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

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
