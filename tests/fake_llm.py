"""假的 OpenAI 相容端點（httpx.MockTransport），模擬 OpenRouter 的串流格式。"""

import json

import httpx


def sse(*chunks: dict | str) -> bytes:
    lines = []
    for c in chunks:
        lines.append(c if isinstance(c, str) else f"data: {json.dumps(c, ensure_ascii=False)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def openrouter_stream(text: str = "你好，", more: str = "世界", reasoning: str = "先想想") -> bytes:
    base = {"id": "gen-1", "model": "z-ai/glm-5.3-flash", "provider": "Z.AI", "object": "chat.completion.chunk"}
    return sse(
        ": OPENROUTER PROCESSING",
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "",
                                                    "reasoning": reasoning,
                                                    "reasoning_details": [{"type": "reasoning.text", "text": reasoning}]}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": text}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": more}, "finish_reason": "stop"}]},
        {**base, "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": 0.000021}},
        "data: [DONE]",
    )


class FakeLLM:
    """依序回應預先排好的 (status, body)；記錄收到的請求。"""

    def __init__(self, *responses: tuple[int, bytes]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            self.requests.append(json.loads(request.content))
            status, body = self.responses.pop(0)
            return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "z-ai/glm-5.3-flash", "name": "GLM 5.3 Flash", "created": 2,
                 "pricing": {"prompt": "0.00000015", "completion": "0.0000005"},
                 "supported_parameters": ["temperature", "top_p", "seed", "max_tokens", "reasoning"]},
            ]})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
