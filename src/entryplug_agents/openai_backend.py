"""Official Responses SDK; API-key and ChatGPT-plan routes remain distinct."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from entryplug_agents.native import ModelReply
from entryplug_app.contracts import AppError


def request_payload(
    model: str,
    instructions: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    chatgpt: bool,
) -> dict[str, Any]:
    # Construct an allowlist; caller-supplied provider parameters are never merged.
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": history,
        "store": False,
        "stream": True,
    }
    if tools:
        payload["tools"] = (
            [
                {
                    "type": "namespace",
                    "name": "entryplug",
                    "description": "Granted local mission tools",
                    "tools": tools,
                }
            ]
            if chatgpt
            else tools
        )
    return payload


class OpenAIBackend:
    capabilities = {
        "tools": True,
        "images": True,
        "streaming": True,
        "cancellation": "request_only",
        "accounting": "provider_reported",
    }

    def __init__(
        self, model: str, credential: Callable[[], Awaitable[str]], *, chatgpt: bool = False
    ):
        self.model, self.credential, self.chatgpt = model, credential, chatgpt

    async def infer(
        self,
        instructions: str,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelReply:
        from openai import APIConnectionError, APIStatusError, AsyncOpenAI

        token = await self.credential()
        payload = request_payload(self.model, instructions, history, tools, chatgpt=self.chatgpt)
        try:
            async with AsyncOpenAI(
                api_key=token, base_url="https://api.openai.com/v1", max_retries=0, timeout=40
            ) as client:
                stream = await client.responses.create(**payload)
                completed = None
                async with stream:
                    async for event in stream:
                        if event.type == "response.output_text.delta" and on_text:
                            await on_text(event.delta)
                        if event.type == "response.completed":
                            completed = event.response
                        elif event.type in {"response.failed", "response.incomplete", "error"}:
                            error = getattr(getattr(event, "response", None), "error", None)
                            code = getattr(error, "code", "inference_incomplete")
                            quota = code in {
                                "subscription_sharing_usage_limit_exceeded",
                                "subscription_sharing_usage_unavailable",
                                "insufficient_quota",
                            }
                            raise AppError(
                                "provider_quota" if quota else "inference_incomplete",
                                "Provider did not complete this turn",
                                409,
                            )
                if completed is None:
                    raise AppError(
                        "inference_interrupted", "Stream ended before response.completed", 409
                    )
                output = [item.model_dump(exclude_none=True) for item in completed.output]
                calls = [item for item in output if item.get("type") == "function_call"]
                for call in calls:
                    # Namespace metadata is not part of a local capability's name.
                    if call["name"].startswith("entryplug."):
                        call["name"] = call["name"][len("entryplug.") :]
                return ModelReply(
                    text=completed.output_text,
                    calls=calls,
                    output=output,
                    usage=completed.usage.model_dump() if completed.usage else {},
                )
        except APIStatusError as error:
            code = (
                "provider_authentication"
                if error.status_code in {401, 403}
                else "provider_quota"
                if error.status_code == 429
                else "provider_unavailable"
            )
            raise AppError(
                code, "Provider requires attention; no fallback or automatic retry was made", 409
            ) from error
        except APIConnectionError as error:
            raise AppError("provider_unavailable", "Inference connection failed", 409) from error
