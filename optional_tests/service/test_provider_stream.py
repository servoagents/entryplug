import json

import httpx2
import openai
import pytest

from entryplug_agents.openai_backend import OpenAIBackend
from entryplug_app.contracts import AppError


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["completed", "quota", "disconnect"])
async def test_official_responses_stream_requires_completion_and_does_not_retry(
    monkeypatch, ending
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        events = [
            {
                "type": "response.output_text.delta",
                "delta": "Unconfirmed partial text",
                "sequence_number": 1,
                "item_id": "i1",
                "output_index": 0,
                "content_index": 0,
            }
        ]
        if ending == "completed":
            events.append(
                {
                    "type": "response.completed",
                    "sequence_number": 2,
                    "response": {
                        "id": "r1",
                        "created_at": 1,
                        "object": "response",
                        "model": "account-model",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "id": "i1",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "Confirmed final text",
                                        "annotations": [],
                                    }
                                ],
                            }
                        ],
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 4,
                            "total_tokens": 16,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    },
                }
            )
        elif ending == "quota":
            events.append(
                {
                    "type": "response.failed",
                    "sequence_number": 2,
                    "response": {
                        "id": "r1",
                        "object": "response",
                        "status": "failed",
                        "output": [],
                        "error": {
                            "code": "subscription_sharing_usage_limit_exceeded",
                            "message": "quota",
                        },
                    },
                }
            )
        body = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    original = openai.AsyncOpenAI
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        lambda **kw: original(
            **kw, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        ),
    )

    async def credential():
        return "fixture-only-token"

    backend = OpenAIBackend("account-model", credential, chatgpt=True)
    if ending == "completed":
        reply = await backend.infer("instructions", [{"role": "user", "content": "Observe"}], [])
        assert reply.text == "Confirmed final text"
        assert reply.usage["total_tokens"] == 16
    else:
        with pytest.raises(AppError) as error:
            await backend.infer("instructions", [{"role": "user", "content": "Observe"}], [])
        assert error.value.code == (
            "provider_quota" if ending == "quota" else "inference_interrupted"
        )
    assert len(requests) == 1
    assert requests[0]["store"] is False and requests[0]["stream"] is True
