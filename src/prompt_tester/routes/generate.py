"""單次生成（SSE 串流）與生成紀錄。"""

import asyncio
import json
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import llm, providers, store
from ..build import BuildResult
from ..entities import Connection
from ..preset import Params, Preset, PreviewContext
from .deps import get_db, get_http
from .prompt import build_for, resolve_scenario

router = APIRouter(prefix="/api")


class GenerateRequest(BaseModel):
    preset: Preset
    context: PreviewContext
    connection_id: int


def request_body(result: BuildResult, params: Params, connection: Connection, seed: int, provider: providers.Provider) -> dict:
    body: dict = {
        "model": connection.model,
        "messages": result.to_api(),
        "temperature": params.temperature,
        "top_p": params.top_p,
        "frequency_penalty": params.frequency_penalty,
        "presence_penalty": params.presence_penalty,
        "max_tokens": params.max_tokens,
    }
    if connection.send_seed:
        body["seed"] = seed
    if provider.is_openrouter:
        if routing := connection.routing.to_body():
            body["provider"] = routing
        if connection.reasoning_effort:
            body["reasoning"] = {"effort": connection.reasoning_effort}
    elif connection.reasoning_effort:
        body["reasoning_effort"] = connection.reasoning_effort
    body.update(connection.extra_body)
    return body


def _estimate_cost(provider: providers.Provider, model: str, usage: dict | None) -> float | None:
    info = providers.cached_model(provider, model)
    if not info or not usage:
        return None
    p = info["pricing"]
    if p["prompt"] is None or p["completion"] is None:
        return None
    return (usage.get("prompt_tokens", 0) * p["prompt"] + usage.get("completion_tokens", 0) * p["completion"]) / 1e6


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/generate")
async def generate(
    req: GenerateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    http: httpx.AsyncClient = Depends(get_http),
) -> StreamingResponse:
    scenario = resolve_scenario(conn, req.context)
    connection = store.get_connection(conn, req.connection_id)
    if connection is None:
        raise HTTPException(404, f"找不到連線 #{req.connection_id}")
    if not connection.model:
        raise HTTPException(422, f"連線「{connection.name}」尚未選擇模型")
    provider = providers.get_provider(connection.provider)
    if not provider.base_url:
        raise HTTPException(422, f"供應商「{connection.provider}」沒有設定 base_url")
    if provider.is_openrouter and not provider.api_key:
        raise HTTPException(422, "尚未設定 OpenRouter API key（設定頁或環境變數 OPENROUTER_API_KEY）")

    result = build_for(req.preset, scenario, req.context)
    body = request_body(result, req.preset.params, connection, req.context.seed, provider)
    preset_hash = store.snapshot_preset(conn, req.preset)
    gid = store.create_generation(
        conn, preset=req.preset, preset_hash=preset_hash, scenario=scenario, connection=connection, request=body
    )

    async def events():
        t0 = llm.now_ms()
        first_ms: int | None = None
        output: list[str] = []
        reasoning: list[str] = []
        usage: dict | None = None
        meta: dict = {}
        finish: str | None = None

        def save(status: str, error: str | None = None, **extra) -> None:
            store.finish_generation(
                conn,
                gid,
                status=status,
                output="".join(output),
                reasoning="".join(reasoning),
                finish_reason=finish,
                usage=usage,
                served_by=meta.get("provider"),
                model=meta.get("model") or connection.model,
                remote_id=meta.get("id"),
                first_token_ms=first_ms,
                latency_ms=llm.now_ms() - t0,
                error=error,
                **extra,
            )

        yield _sse({"type": "start", "id": gid, "message_count": len(result.messages), "warnings": result.warnings})
        try:
            async for ev in llm.stream_chat(http, provider, body):
                kind = ev["type"]
                if kind in ("content", "reasoning"):
                    if first_ms is None:
                        first_ms = llm.now_ms() - t0
                    (output if kind == "content" else reasoning).append(ev["text"])
                    yield _sse(ev)
                elif kind == "meta":
                    meta = ev
                    yield _sse(ev)
                elif kind == "finish":
                    finish = ev["reason"]
                elif kind == "usage":
                    usage = ev["usage"]
        except llm.LLMError as e:
            save("error", str(e))
            yield _sse({"type": "error", "message": str(e)})
            return
        except asyncio.CancelledError:
            save("cancelled", "使用者中止")
            raise

        cost, cost_source = (usage or {}).get("cost"), "api"
        if cost is None or not meta.get("provider"):
            stats = await llm.fetch_generation_stats(http, provider, meta.get("id") or "")
            if stats:
                meta["provider"] = meta.get("provider") or stats.get("provider_name")
                if cost is None and stats.get("total_cost") is not None:
                    cost, cost_source = stats["total_cost"], "generation"
        if cost is None:
            cost = _estimate_cost(provider, connection.model, usage)
            cost_source = "estimate" if cost is not None else None

        save("done", cost=cost, cost_source=cost_source)
        yield _sse({"type": "done", "generation": store.get_generation(conn, gid)})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/generations")
def list_generations(
    limit: int = 30, scenario_id: int | None = None, conn: sqlite3.Connection = Depends(get_db)
) -> list[dict]:
    return store.list_generations(conn, min(limit, 200), scenario_id)


@router.get("/generations/{gid}")
def get_generation(gid: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    g = store.get_generation(conn, gid)
    if g is None:
        raise HTTPException(404, f"找不到生成紀錄 #{gid}")
    return g


@router.delete("/generations/{gid}", status_code=204)
def delete_generation(gid: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    store.delete_generation(conn, gid)
