import asyncio

import httpx
import pytest

from prompt_tester import llm
from prompt_tester.providers import Provider

from fake_llm import FakeLLM, openrouter_stream, sse

PROVIDER = Provider("openrouter", "https://openrouter.ai/api/v1", "k", "config")


def collect(fake: FakeLLM) -> list[dict]:
    async def run():
        async with httpx.AsyncClient(transport=fake.transport) as client:
            return [ev async for ev in llm.stream_chat(client, PROVIDER, {"model": "m", "messages": []}, backoff=0)]

    return asyncio.run(run())


def test_parses_openrouter_stream():
    fake = FakeLLM((200, openrouter_stream()))
    events = collect(fake)
    assert events[0] == {"type": "meta", "id": "gen-1", "model": "z-ai/glm-5.3-flash", "provider": "Z.AI"}
    # reasoning_details 優先，不與 reasoning 字串重複
    assert [e["text"] for e in events if e["type"] == "reasoning"] == ["先想想"]
    assert "".join(e["text"] for e in events if e["type"] == "content") == "你好，世界"
    assert [e for e in events if e["type"] == "usage"][0]["usage"]["cost"] == 0.000021
    assert fake.requests[0]["stream"] is True


def test_deepseek_reasoning_content():
    fake = FakeLLM((200, sse({"id": "x", "choices": [{"delta": {"reasoning_content": "想"}}]}, "data: [DONE]")))
    assert [e["text"] for e in collect(fake) if e["type"] == "reasoning"] == ["想"]


def test_retries_429_before_stream_starts():
    fake = FakeLLM((429, b'{"error":{"message":"rate limited"}}'), (200, openrouter_stream()))
    events = collect(fake)
    assert len(fake.requests) == 2
    assert any(e["type"] == "content" for e in events)


def test_non_retryable_http_error():
    fake = FakeLLM((401, b'{"error":{"message":"bad key"}}'))
    with pytest.raises(llm.LLMError, match="401.*bad key"):
        collect(fake)
    assert len(fake.requests) == 1


def test_error_chunk_after_start_is_not_retried():
    body = sse(
        {"id": "g", "model": "m", "choices": [{"delta": {"content": "半"}}]},
        {"id": "g", "model": "m", "error": {"code": 502, "message": "provider died"},
         "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]},
    )
    fake = FakeLLM((200, body), (200, openrouter_stream()))
    with pytest.raises(llm.LLMError, match="provider died"):
        collect(fake)
    assert len(fake.requests) == 1


def test_error_only_stream_is_retried():
    body = sse({"id": "g", "model": "m", "error": {"code": 503, "message": "busy"}})
    fake = FakeLLM((200, body), (200, openrouter_stream()))
    assert any(e["type"] == "content" for e in collect(fake))
    assert len(fake.requests) == 2
