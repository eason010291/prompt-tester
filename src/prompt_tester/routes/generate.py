"""單次生成（SSE 串流）與生成紀錄。"""

import asyncio
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import llm, providers, store, vcs
from ..preset import Preset, PreviewContext
from ..runner import estimate_cost, request_body, sse
from .deps import get_db, get_http
from .prompt import build_for, resolve_scenario

router = APIRouter(prefix="/api")


class GenerateRequest(BaseModel):
    preset: Preset
    context: PreviewContext
    connection_id: int


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
    body = request_body(result.to_api(), req.preset.params, connection, req.context.seed, provider)
    preset_hash = store.snapshot_preset(conn, req.preset)
    # 送出的預設剛好等於某個 commit 時記下，日後可追溯到版本
    commit_id = vcs.commit_for_tree(conn, vcs.tree_id_of(req.preset))
    gid = store.create_generation(
        conn,
        preset=req.preset,
        preset_hash=preset_hash,
        scenario=scenario,
        connection=connection,
        request=body,
        commit_id=commit_id,
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

        yield sse({"type": "start", "id": gid, "message_count": len(result.messages), "warnings": result.warnings})
        try:
            async for ev in llm.stream_chat(http, provider, body):
                kind = ev["type"]
                if kind in ("content", "reasoning"):
                    if first_ms is None:
                        first_ms = llm.now_ms() - t0
                    (output if kind == "content" else reasoning).append(ev["text"])
                    yield sse(ev)
                elif kind == "meta":
                    meta = ev
                    yield sse(ev)
                elif kind == "finish":
                    finish = ev["reason"]
                elif kind == "usage":
                    usage = ev["usage"]
        except llm.LLMError as e:
            save("error", str(e))
            yield sse({"type": "error", "message": str(e)})
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
            cost = estimate_cost(provider, connection.model, usage)
            cost_source = "estimate" if cost is not None else None

        save("done", cost=cost, cost_source=cost_source)
        yield sse({"type": "done", "generation": store.get_generation(conn, gid)})

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
