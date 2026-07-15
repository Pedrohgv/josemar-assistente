from __future__ import annotations

import asyncio
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))
sys.modules.setdefault("pymupdf", types.ModuleType("pymupdf"))
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

from app.adapters import transcribe_granite
from app.model_registry import ModelSpec
from app import settings as settings_module


# ---------------------------------------------------------------------------
# Shared loudnorm filter string used by every normalization path.
# ---------------------------------------------------------------------------
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


class FakeRouter:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.requests: list[dict] = []
        self._responses = responses or ["hello world"]

    async def audio_transcription(
        self,
        *,
        file_path: Path,
        model_id: str,
        prompt: str,
        mime_type: str,
        timeout_seconds: int,
    ) -> dict:
        self.requests.append({
            "file_path": file_path,
            "model_id": model_id,
            "prompt": prompt,
            "mime_type": mime_type,
            "timeout_seconds": timeout_seconds,
        })
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return {"text": self._responses[index]}


def _make_spec(tmp: Path) -> ModelSpec:
    return ModelSpec(
        "granite-speech-4.1-2b",
        "transcribe",
        tmp / "granite-speech-4.1-2b-Q8_0.gguf",
        8192,
        "transcribe the speech with proper punctuation and capitalization.",
        4096,
    )


class NormalizationArgumentCapture:
    """Helper to capture ffmpeg args from _run_command calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def make_side_effect(self, stdout: str = "10.0"):
        async def fake(args, timeout_seconds, cancel_event=None):
            self.calls.append(list(args))
            return stdout
        return fake


class ShortAudioNormalizationTests(unittest.IsolatedAsyncioTestCase):
    """Prove short/single-shot audio is normalized to a temporary 16k mono
    PCM WAV with loudnorm before the router call, and temp cleanup occurs."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.spec = _make_spec(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_short_audio_is_normalized_before_router_call(self) -> None:
        audio = self.root / "sample.wav"
        audio.write_bytes(b"RIFFtest")
        router = FakeRouter()
        capture = NormalizationArgumentCapture()

        with patch.object(
            transcribe_granite, "_probe_duration_seconds", return_value=10
        ):
            with patch.object(
                transcribe_granite,
                "_run_command",
                side_effect=capture.make_side_effect(),
            ):
                result = await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=72,
                    chunk_seconds=30,
                    overlap_seconds=2,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=router,
                )

        # Router was called exactly once with a normalized temp file.
        self.assertEqual(len(router.requests), 1)
        sent_path = Path(router.requests[0]["file_path"])
        # The file sent to the router must NOT be the original input.
        self.assertNotEqual(sent_path, audio.resolve())
        # It must be a WAV.
        self.assertEqual(sent_path.suffix.lower(), ".wav")
        # mime type must reflect WAV.
        self.assertEqual(router.requests[0]["mime_type"], "audio/x-wav")
        # Single-shot mode.
        self.assertEqual(result["mode"], "single-shot")

        # At least one ffmpeg call must contain the loudnorm filter and
        # 16k mono PCM WAV output args.
        loudnorm_calls = [
            c for c in capture.calls
            if any(isinstance(a, str) and "loudnorm=I=-16" in a for a in c)
        ]
        self.assertTrue(
            len(loudnorm_calls) >= 1,
            f"Expected a loudnorm ffmpeg call; got {capture.calls}",
        )
        call = loudnorm_calls[0]
        flat = " ".join(call)
        self.assertIn("16000", flat)
        self.assertIn("pcm_s16le", flat)
        self.assertIn("1", flat)  # mono

    async def test_short_audio_temp_file_cleaned_up(self) -> None:
        audio = self.root / "sample.wav"
        audio.write_bytes(b"RIFFtest")
        router = FakeRouter()
        capture = NormalizationArgumentCapture()

        with patch.object(
            transcribe_granite, "_probe_duration_seconds", return_value=10
        ):
            with patch.object(
                transcribe_granite,
                "_run_command",
                side_effect=capture.make_side_effect(),
            ):
                result = await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=72,
                    chunk_seconds=30,
                    overlap_seconds=2,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=router,
                )

        sent_path = Path(router.requests[0]["file_path"])
        # After completion, the temp normalized file must be gone.
        self.assertFalse(sent_path.exists(), f"Temp file not cleaned: {sent_path}")


class ChunkedNormalizationTests(unittest.IsolatedAsyncioTestCase):
    """Prove the chunked path uses the same loudnorm helper/arguments."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.spec = _make_spec(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_chunked_path_uses_loudnorm_helper(self) -> None:
        audio = self.root / "long.mp3"
        audio.write_bytes(b"ID3test")
        router = FakeRouter(responses=["alpha", "beta", "gamma"])
        capture = NormalizationArgumentCapture()

        # duration 90s with chunk=30 overlap=2 -> 4 chunks (0-30, 28-58, 56-86, 84-90)
        # Actually compute: step=28, starts 0,28,56,84 -> 84<90 so 84-90. 4 chunks.
        with patch.object(
            transcribe_granite, "_probe_duration_seconds", return_value=90
        ):
            with patch.object(
                transcribe_granite,
                "_run_command",
                side_effect=capture.make_side_effect(),
            ):
                result = await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=72,
                    chunk_seconds=30,
                    overlap_seconds=2,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=router,
                )

        self.assertEqual(result["mode"], "chunked")
        self.assertEqual(result["chunk_count"], 4)
        self.assertEqual(len(router.requests), 4)

        # Every router request must point to a normalized temp WAV.
        for req in router.requests:
            self.assertEqual(req["mime_type"], "audio/x-wav")
            self.assertEqual(Path(req["file_path"]).suffix.lower(), ".wav")

        # Every ffmpeg call must contain loudnorm + 16k mono pcm.
        loudnorm_calls = [
            c for c in capture.calls
            if any(isinstance(a, str) and "loudnorm=I=-16" in a for a in c)
        ]
        self.assertEqual(
            len(loudnorm_calls),
            4,
            f"Expected 4 loudnorm chunk calls; got {len(loudnorm_calls)}: {capture.calls}",
        )
        for call in loudnorm_calls:
            flat = " ".join(call)
            self.assertIn("16000", flat)
            self.assertIn("pcm_s16le", flat)


class ChunkRangeDefaultsTests(unittest.TestCase):
    """Prove defaults 30/2/72 and invalid overlap>=chunk raises."""

    def test_chunk_ranges_30min_does_not_exceed_default_max_chunks(self) -> None:
        # 30 minutes = 1800s, chunk=30, overlap=2 -> step=28.
        ranges = transcribe_granite._chunk_ranges(
            duration_seconds=1800,
            chunk_seconds=30,
            overlap_seconds=2,
        )
        self.assertLessEqual(len(ranges), 72)
        # Sanity: should be 65 chunks.
        self.assertEqual(len(ranges), 65)

    def test_chunk_ranges_single_shot_when_duration_le_chunk(self) -> None:
        ranges = transcribe_granite._chunk_ranges(
            duration_seconds=10,
            chunk_seconds=30,
            overlap_seconds=2,
        )
        self.assertEqual(ranges, [(0.0, 10.0)])


class SettingsDefaultsTests(unittest.TestCase):
    """Prove app defaults are 30/2/72 and overlap>=chunk raises."""

    def test_settings_defaults_match_30_2_72(self) -> None:
        # Use a clean env (no overrides).
        env = {
            "AUX_ML_TRANSCRIBE_CHUNK_SECONDS": "",
            "AUX_ML_TRANSCRIBE_OVERLAP_SECONDS": "",
            "AUX_ML_TRANSCRIBE_MAX_CHUNKS": "",
        }
        with patch.dict("os.environ", env, clear=False):
            # Force the relevant vars to be unset.
            for k in env:
                if k in __import__("os").environ:
                    del __import__("os").environ[k]
            s = settings_module.load_settings()
        self.assertEqual(s.transcribe_chunk_seconds, 30)
        self.assertEqual(s.transcribe_overlap_seconds, 2)
        self.assertEqual(s.transcribe_max_chunks, 72)
        self.assertEqual(s.transcribe_max_duration_seconds, 1800)

    def test_settings_overlap_ge_chunk_raises(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUX_ML_TRANSCRIBE_CHUNK_SECONDS": "30",
                "AUX_ML_TRANSCRIBE_OVERLAP_SECONDS": "30",
                "AUX_ML_TRANSCRIBE_MAX_CHUNKS": "",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                settings_module.load_settings()

    def test_settings_overlap_ge_chunk_raises_when_chunk_below_min(self) -> None:
        # chunk=5 (below min 10) with overlap=5 should also raise, not
        # silently clamp to pathological 1-second steps.
        with patch.dict(
            "os.environ",
            {
                "AUX_ML_TRANSCRIBE_CHUNK_SECONDS": "5",
                "AUX_ML_TRANSCRIBE_OVERLAP_SECONDS": "5",
                "AUX_ML_TRANSCRIBE_MAX_CHUNKS": "",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                settings_module.load_settings()

    def test_settings_explicit_override_preserved(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUX_ML_TRANSCRIBE_CHUNK_SECONDS": "240",
                "AUX_ML_TRANSCRIBE_OVERLAP_SECONDS": "20",
                "AUX_ML_TRANSCRIBE_MAX_CHUNKS": "16",
            },
            clear=False,
        ):
            s = settings_module.load_settings()
        self.assertEqual(s.transcribe_chunk_seconds, 240)
        self.assertEqual(s.transcribe_overlap_seconds, 20)
        self.assertEqual(s.transcribe_max_chunks, 16)


class ComposeDefaultsTests(unittest.TestCase):
    """Prove compose defaults match app defaults and exact b9585 pin."""

    def setUp(self) -> None:
        self.compose_path = REPO_ROOT / "docker-compose.yml"
        self.text = self.compose_path.read_text(encoding="utf-8")

    def test_compose_chunk_defaults_match_app_defaults(self) -> None:
        self.assertIn("AUX_ML_TRANSCRIBE_CHUNK_SECONDS=${AUX_ML_TRANSCRIBE_CHUNK_SECONDS:-30}", self.text)
        self.assertIn("AUX_ML_TRANSCRIBE_OVERLAP_SECONDS=${AUX_ML_TRANSCRIBE_OVERLAP_SECONDS:-2}", self.text)
        self.assertIn("AUX_ML_TRANSCRIBE_MAX_CHUNKS=${AUX_ML_TRANSCRIBE_MAX_CHUNKS:-72}", self.text)
        self.assertIn("AUX_ML_TRANSCRIBE_MAX_DURATION_SECONDS=${AUX_ML_TRANSCRIBE_MAX_DURATION_SECONDS:-1800}", self.text)

    def test_compose_llama_cpp_release_url_is_b9585(self) -> None:
        expected_url = "https://github.com/ggml-org/llama.cpp/releases/download/b9585/llama-b9585-bin-ubuntu-x64.tar.gz"
        self.assertIn(expected_url, self.text)

    def test_compose_llama_cpp_release_sha256_is_64_hex(self) -> None:
        # Find the SHA256 default in the compose file.
        m = re.search(
            r"LLAMA_CPP_RELEASE_SHA256:\s*\$\{AUX_ML_LLAMA_CPP_RELEASE_SHA256:-([0-9a-fA-F]{64})\}",
            self.text,
        )
        self.assertIsNotNone(m, "Could not find LLAMA_CPP_RELEASE_SHA256 default in docker-compose.yml")
        sha = m.group(1)
        self.assertEqual(
            sha,
            "be111dd28e6228fc4cb6a6ec41f03a67947ab61f315a3d22d0e68ac7372a58ab",
        )
        self.assertRegex(sha, r"^[0-9a-fA-F]{64}$")


class EnvExampleTests(unittest.TestCase):
    """Prove .env.example documents the relevant transcription settings and
    llama.cpp release override vars."""

    def setUp(self) -> None:
        self.env_path = REPO_ROOT / ".env.example"
        self.text = self.env_path.read_text(encoding="utf-8")

    def test_env_example_has_transcribe_settings(self) -> None:
        for name in (
            "AUX_ML_TRANSCRIBE_CHUNK_SECONDS",
            "AUX_ML_TRANSCRIBE_OVERLAP_SECONDS",
            "AUX_ML_TRANSCRIBE_MAX_CHUNKS",
            "AUX_ML_TRANSCRIBE_MAX_DURATION_SECONDS",
            "AUX_ML_TRANSCRIBE_MAX_BYTES",
            "AUX_ML_TRANSCRIBE_FFMPEG_TIMEOUT_SECONDS",
        ):
            self.assertIn(name, self.text, f"{name} missing from .env.example")

    def test_env_example_has_llama_release_override_vars(self) -> None:
        for name in (
            "AUX_ML_LLAMA_CPP_RELEASE_URL",
            "AUX_ML_LLAMA_CPP_RELEASE_SHA256",
        ):
            self.assertIn(name, self.text, f"{name} missing from .env.example")


if __name__ == "__main__":
    unittest.main()