from __future__ import annotations

from pathlib import Path
import asyncio

import httpx


class RouterError(RuntimeError):
    pass


class LlamaRouterClient:
    def __init__(self, base_url: str, default_timeout_seconds: int) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=default_timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _error_text(self, response: httpx.Response) -> str:
        try:
            text = response.text
        except UnicodeDecodeError:
            text = response.content.decode("utf-8", errors="replace")
        text = text.replace("\n", " ").strip()
        if len(text) > 500:
            return f"{text[:500]}..."
        return text

    async def ping(self) -> bool:
        try:
            await self.list_models()
            return True
        except Exception:
            return False

    async def list_models(self) -> list[dict]:
        response = await self._client.get("/models")
        if response.status_code >= 400:
            raise RouterError(f"/models failed ({response.status_code}): {self._error_text(response)}")

        payload = response.json()
        if isinstance(payload, dict):
            models = payload.get("data")
            if isinstance(models, list):
                return models
            models = payload.get("models")
            if isinstance(models, list):
                return models

        raise RouterError("Unexpected /models response payload")

    async def resolve_model_id(self, model_path: Path) -> str:
        target = str(model_path)
        target_name = model_path.name
        target_stem = model_path.stem

        models = await self.list_models()
        for model in models:
            model_id = model.get("id")
            path = str(model.get("path", ""))
            if not model_id:
                continue
            if path == target or path.endswith(target_name):
                return str(model_id)
            if str(model_id) in {target_stem, target_name, target}:
                return str(model_id)

            status = model.get("status")
            if isinstance(status, dict):
                args = status.get("args")
                if isinstance(args, list) and target in {str(arg) for arg in args}:
                    return str(model_id)

        raise RouterError(f"Model path '{target}' not found in llama router model list")

    async def load_model(self, model_id: str) -> None:
        response = await self._client.post("/models/load", json={"model": model_id})
        if response.status_code >= 400:
            raise RouterError(f"/models/load failed ({response.status_code}): {self._error_text(response)}")

    async def unload_model(self, model_id: str) -> None:
        response = await self._client.post("/models/unload", json={"model": model_id})
        if response.status_code >= 400:
            raise RouterError(f"/models/unload failed ({response.status_code}): {self._error_text(response)}")

    async def wait_for_status(
        self,
        model_id: str,
        expected: set[str],
        timeout_seconds: int,
        poll_interval_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            models = await self.list_models()
            for model in models:
                if model.get("id") != model_id:
                    continue

                status_payload = model.get("status")
                if isinstance(status_payload, dict):
                    status_value = str(status_payload.get("value", ""))
                    failed = bool(status_payload.get("failed", False))
                    if failed:
                        exit_code = status_payload.get("exit_code")
                        raise RouterError(
                            f"Model '{model_id}' failed to transition state (exit_code={exit_code})"
                        )
                else:
                    status_value = str(status_payload or "")

                if status_value in expected:
                    return
                break

            if asyncio.get_running_loop().time() >= deadline:
                expected_sorted = ", ".join(sorted(expected))
                raise RouterError(
                    f"Timed out waiting for model '{model_id}' status in {{{expected_sorted}}}"
                )

            await asyncio.sleep(poll_interval_seconds)

    async def chat_completion(self, payload: dict, timeout_seconds: int) -> dict:
        response = await self._client.post(
            "/v1/chat/completions",
            json=payload,
            timeout=timeout_seconds,
        )
        if response.status_code >= 400:
            raise RouterError(
                f"/v1/chat/completions failed ({response.status_code}): {self._error_text(response)}"
            )
        return response.json()

    async def completion(self, payload: dict, timeout_seconds: int) -> dict:
        response = await self._client.post(
            "/completion",
            json=payload,
            timeout=timeout_seconds,
        )
        if response.status_code >= 400:
            raise RouterError(
                f"/completion failed ({response.status_code}): {self._error_text(response)}"
            )
        return response.json()

    async def audio_transcription(
        self,
        *,
        file_path: Path,
        model_id: str,
        prompt: str,
        mime_type: str,
        timeout_seconds: int,
    ) -> dict:
        data = {
            "model": model_id,
            "prompt": prompt,
        }
        with file_path.open("rb") as audio_file:
            files = {"file": (file_path.name, audio_file, mime_type)}
            response = await self._client.post(
                "/v1/audio/transcriptions",
                data=data,
                files=files,
                timeout=timeout_seconds,
            )

        if response.status_code >= 400:
            raise RouterError(
                f"/v1/audio/transcriptions failed ({response.status_code}): {self._error_text(response)}"
            )
        return response.json()
