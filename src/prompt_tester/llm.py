"""OpenAI 相容 Chat Completion 串流 client。

處理 OpenRouter 與一般 OpenAI 相容端點的差異（依 OpenRouter 文件，2026-10）：
- SSE 中 `:` 開頭的保活註解行要略過；`data: [DONE]` 結束
- 已開始串流後的錯誤：HTTP 仍是 200，事件帶頂層 `error`，`finish_reason: "error"`
- 推理內容：OpenRouter `delta.reasoning_details`（reasoning.text / reasoning.summary），
  也可能有 `delta.reasoning` 字串；DeepSeek 原生為 `delta.reasoning_content`
- usage 在最後一個 chunk（OpenRouter 含 cost），不需要 stream_options
重試只發生在收到第一個事件之前（429 / 5xx / 連線錯誤），避免重複計費與重複輸出。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

import httpx

from .providers import Provider

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# 串流卡住的判定：OpenRouter 在處理中會持續送 `: OPENROUTER PROCESSING` 保活註解，
# 會讓 httpx 的讀取逾時永遠不觸發，因此另外計算「多久沒收到真正的資料」與單次呼叫的總時間。
STALL_SECONDS = 180
MAX_CALL_SECONDS = 900
APP_HEADERS = {"HTTP-Referer": "http://localhost/prompt-tester", "X-OpenRouter-Title": "Prompt Tester"}


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _reasoning_text(delta: dict) -> str:
    details = delta.get("reasoning_details")
    if isinstance(details, list) and details:
        parts = []
        for d in details:
            if d.get("type") == "reasoning.text":
                parts.append(d.get("text") or "")
            elif d.get("type") == "reasoning.summary":
                parts.append(d.get("summary") or "")
        return "".join(parts)
    return delta.get("reasoning") or delta.get("reasoning_content") or ""


def _error_message(payload: dict | str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
            raw = (err.get("metadata") or {}).get("raw")
            return f"{msg}（{raw}）" if raw and raw not in msg else msg
        return str(err)
    return payload


async def stream_chat(
    client: httpx.AsyncClient,
    provider: Provider,
    body: dict,
    *,
    max_retries: int = 2,
    backoff: float = 1.0,
) -> AsyncIterator[dict]:
    """產生事件：
    {"type": "meta", "id", "model", "provider"}       第一次看到時
    {"type": "content" | "reasoning", "text"}
    {"type": "finish", "reason"}
    {"type": "usage", "usage"}
    """
    headers = {"Content-Type": "application/json", **APP_HEADERS}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    body = {**body, "stream": True}

    attempt = 0
    while True:
        started = False
        try:
            async with client.stream(
                "POST", f"{provider.base_url}/chat/completions", json=body, headers=headers, timeout=httpx.Timeout(30, read=300)
            ) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode("utf-8", "replace")
                    try:
                        msg = _error_message(json.loads(text))
                    except ValueError:
                        msg = text[:500] or r.reason_phrase
                    raise LLMError(f"HTTP {r.status_code}：{msg}", r.status_code, r.status_code in RETRY_STATUS)

                meta_sent = False
                gen_id = r.headers.get("x-generation-id")
                t_start = last_data = time.monotonic()
                async for line in r.aiter_lines():
                    now = time.monotonic()
                    if now - last_data > STALL_SECONDS:
                        raise LLMError(f"超過 {STALL_SECONDS} 秒沒有收到內容（上游可能卡住），已中斷", None, retryable=not started)
                    if now - t_start > MAX_CALL_SECONDS:
                        raise LLMError(f"單次呼叫超過 {MAX_CALL_SECONDS // 60} 分鐘，已中斷", None, retryable=False)
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    last_data = now
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue

                    if "error" in chunk:
                        raise LLMError(_error_message(chunk), None, retryable=not started)

                    if not meta_sent and (chunk.get("id") or chunk.get("model")):
                        meta_sent = True
                        started = True
                        yield {
                            "type": "meta",
                            "id": gen_id or chunk.get("id"),
                            "model": chunk.get("model"),
                            "provider": chunk.get("provider"),
                        }

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if reasoning := _reasoning_text(delta):
                            started = True
                            yield {"type": "reasoning", "text": reasoning}
                        if content := delta.get("content"):
                            started = True
                            yield {"type": "content", "text": content}
                        if reason := choice.get("finish_reason"):
                            yield {"type": "finish", "reason": reason}

                    if usage := chunk.get("usage"):
                        yield {"type": "usage", "usage": usage}
                return
        except LLMError as e:
            if started or not e.retryable or attempt >= max_retries:
                raise
        except httpx.TransportError as e:
            if started or attempt >= max_retries:
                raise LLMError(f"連線錯誤：{e.__class__.__name__} {e}") from e
        attempt += 1
        await asyncio.sleep(backoff * 2 ** (attempt - 1))


async def chat_complete(
    client: httpx.AsyncClient,
    provider: Provider,
    body: dict,
    *,
    max_retries: int = 2,
    backoff: float = 1.0,
) -> dict:
    """非串流呼叫（tool calling 用：tool_calls 與 reasoning_details 需完整取得後原樣送回）。回傳完整回應 JSON。"""
    headers = {"Content-Type": "application/json", **APP_HEADERS}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    body = {**body, "stream": False}
    attempt = 0
    while True:
        try:
            r = await client.post(f"{provider.base_url}/chat/completions", json=body, headers=headers,
                                  timeout=httpx.Timeout(30, read=600))
            text = r.text
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if r.status_code >= 400 or data is None:
                msg = _error_message(data) if isinstance(data, dict) else (text[:500] or r.reason_phrase)
                raise LLMError(f"HTTP {r.status_code}：{msg}", r.status_code, r.status_code in RETRY_STATUS)
            if "error" in data and not data.get("choices"):
                # OpenRouter 上游錯誤可能以 200 + error 回傳
                raise LLMError(_error_message(data), None, retryable=True)
            return data
        except LLMError as e:
            if not e.retryable or attempt >= max_retries:
                raise
        except httpx.TransportError as e:
            if attempt >= max_retries:
                raise LLMError(f"連線錯誤：{e.__class__.__name__} {e}") from e
        attempt += 1
        await asyncio.sleep(backoff * 2 ** (attempt - 1))


async def fetch_generation_stats(client: httpx.AsyncClient, provider: Provider, gen_id: str) -> dict | None:
    """OpenRouter /generation：實際服務的供應商與精確費用。統計會延遲幾秒才可查，失敗回傳 None。"""
    if not provider.is_openrouter or not gen_id or not provider.api_key:
        return None
    for delay in (0.5, 1.5, 3.0):
        await asyncio.sleep(delay)
        try:
            r = await client.get(
                f"{provider.base_url}/generation",
                params={"id": gen_id},
                headers={"Authorization": f"Bearer {provider.api_key}"},
                timeout=15,
            )
        except httpx.TransportError:
            continue
        if r.status_code == 200:
            return r.json().get("data")
    return None


def now_ms() -> int:
    return int(time.perf_counter() * 1000)
