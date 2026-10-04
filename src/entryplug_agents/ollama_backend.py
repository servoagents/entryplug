"""Local Ollama Responses transport; all actions still pass through NativeDriver."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from entryplug_agents.native import ModelReply
from entryplug_app.contracts import AppError

OLLAMA_URL = "http://127.0.0.1:11434"


async def request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=90 if path == "/v1/responses" else 5, trust_env=False
        ) as client:
            response = (
                await client.get(OLLAMA_URL + path)
                if payload is None
                else await client.post(OLLAMA_URL + path, json=payload)
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            return value
    except httpx.HTTPStatusError as error:
        raise AppError(
            "provider_unavailable",
            "Ollama refused the request. Check the installed model and Ollama version.",
            409,
        ) from error
    except (httpx.RequestError, ValueError) as error:
        raise AppError(
            "provider_unavailable",
            "Cannot reach local Ollama. Start it on 127.0.0.1:11434 and try again.",
            409,
        ) from error


def is_local(model: dict[str, Any]) -> bool:
    return not (
        model.get("remote_host")
        or model.get("remote_model")
        or "cloud" in model.get("name", "").split(":")
        or model.get("name", "").endswith("-cloud")
    )


async def local_models() -> list[dict[str, str]]:
    value = await request("/api/tags")
    return [
        {"id": m["name"], "name": m["name"]}
        for m in value.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("name"), str) and is_local(m)
    ]


async def check_model(model: str) -> None:
    value = await request("/api/show", {"model": model})
    if not is_local({**value, "name": model}):
        raise AppError("provider_unsupported", "Choose a downloaded local model, not a cloud model")
    if "tools" not in value.get("capabilities", []):
        raise AppError("model_tools_required", "Choose an Ollama model with tool calling support")


class OllamaBackend:
    capabilities = {
        "tools": True,
        "images": False,
        "streaming": False,
        "cancellation": "request_only",
        "accounting": "provider_reported",
    }

    def __init__(self, model: str):
        self.model = model

    async def infer(
        self,
        instructions: str,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelReply:
        # Stateless Responses preserves call IDs and tool results across the same
        # bounded loop as other providers. No fallback to a hosted endpoint.
        response = await request(
            "/v1/responses",
            {
                "model": self.model,
                "instructions": instructions,
                "input": history,
                "tools": tools,
                "stream": False,
                "store": False,
                "max_output_tokens": 1024,
            },
        )
        if response.get("status") != "completed":
            raise AppError("inference_incomplete", "Ollama did not complete this turn", 409)
        output = response.get("output")
        if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
            raise AppError("inference_incomplete", "Ollama returned an invalid response", 409)
        calls = [item for item in output if item.get("type") == "function_call"]
        if any(not isinstance(call.get("call_id"), str) or not call["call_id"] for call in calls):
            raise AppError("inference_incomplete", "Ollama returned a tool call without an ID", 409)
        message = "\n".join(
            part["text"]
            for item in output
            if item.get("type") == "message"
            for part in item.get("content", [])
            if isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        )
        if message and on_text:
            await on_text(message)
        return ModelReply(
            text=message, calls=calls, output=output, usage=response.get("usage") or {}
        )
