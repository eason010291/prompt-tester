"""多回合試跑（LLM 扮演 {{user}} / 回放）與模擬使用者設定。"""

import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import providers, store, vcs
from ..preset import Preset
from ..runner import RunSpec, UserSimSettings, run_session
from .deps import get_db, get_http

router = APIRouter(prefix="/api")

USER_SIM_KEY = "user_sim"
MAX_TURNS = 12


def load_user_sim(conn: sqlite3.Connection) -> UserSimSettings:
    return UserSimSettings.model_validate(store.get_setting(conn, USER_SIM_KEY) or {})


@router.get("/user-sim")
def get_user_sim(conn: sqlite3.Connection = Depends(get_db)) -> UserSimSettings:
    return load_user_sim(conn)


@router.put("/user-sim")
def put_user_sim(body: UserSimSettings, conn: sqlite3.Connection = Depends(get_db)) -> UserSimSettings:
    store.set_setting(conn, USER_SIM_KEY, body.model_dump())
    return body


class RunRequest(BaseModel):
    preset: Preset
    scenario_id: int
    connection_id: int
    turns: int = Field(3, ge=1, le=MAX_TURNS)
    seed: int = 1
    mode: str = "live"
    # 回放模式：沿用這次試跑的使用者發言
    replay_of: int | None = None
    # 第 1 回合優先使用情境的第一句使用者輸入（沒有時才由 LLM 產生）
    first_from_scenario: bool = True


def _provider_for(conn: sqlite3.Connection, connection_id: int):
    c = store.get_connection(conn, connection_id)
    if c is None:
        raise HTTPException(404, f"找不到連線 #{connection_id}")
    if not c.model:
        raise HTTPException(422, f"連線「{c.name}」尚未選擇模型")
    p = providers.get_provider(c.provider)
    if p.is_openrouter and not p.api_key:
        raise HTTPException(422, "尚未設定 OpenRouter API key（設定頁或環境變數 OPENROUTER_API_KEY）")
    return c, p


@router.post("/runs")
async def start_run(
    req: RunRequest,
    conn: sqlite3.Connection = Depends(get_db),
    http: httpx.AsyncClient = Depends(get_http),
) -> StreamingResponse:
    if req.mode not in ("live", "replay"):
        raise HTTPException(422, "mode 必須是 live 或 replay")
    scenario = store.get_scenario(conn, req.scenario_id)
    if scenario is None:
        raise HTTPException(404, f"找不到情境 #{req.scenario_id}")
    connection, provider = _provider_for(conn, req.connection_id)

    replay: list[str] = []
    if req.mode == "replay":
        if req.replay_of is None or store.get_run(conn, req.replay_of) is None:
            raise HTTPException(422, "回放模式需要指定要沿用哪一次試跑")
        replay = store.run_user_turns(conn, req.replay_of)

    settings = load_user_sim(conn)
    user_conn = user_provider = None
    needs_llm_user = req.mode == "live" or req.turns > len(replay)
    if needs_llm_user:
        if settings.connection_id is None:
            raise HTTPException(422, "尚未設定模擬使用者的連線（設定頁 → 模擬使用者）")
        user_conn, user_provider = _provider_for(conn, settings.connection_id)

    preset_hash = store.snapshot_preset(conn, req.preset)
    run_id = store.create_run(
        conn,
        mode=req.mode,
        replay_of=req.replay_of if req.mode == "replay" else None,
        turns=req.turns,
        seed=req.seed,
        preset=req.preset,
        preset_hash=preset_hash,
        commit_id=vcs.commit_for_tree(conn, vcs.tree_id_of(req.preset)),
        scenario=scenario,
        connection=connection,
        user_connection=user_conn,
        user_settings=settings.model_dump() if needs_llm_user else None,
    )
    spec = RunSpec(
        preset=req.preset,
        scenario=scenario,
        connection=connection,
        provider=provider,
        turns=req.turns,
        seed=req.seed,
        mode=req.mode,
        replay_user_turns=replay,
        user_connection=user_conn,
        user_provider=user_provider,
        user_settings=settings,
        first_from_scenario=req.first_from_scenario,
    )
    return StreamingResponse(run_session(conn, http, spec, run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.get("/runs")
def list_runs(scenario_id: int | None = None, limit: int = 30, conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return store.list_runs(conn, scenario_id, min(limit, 200))


@router.get("/runs/{run_id}")
def get_run(run_id: int, requests: bool = False, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    run = store.get_run(conn, run_id, with_requests=requests)
    if run is None:
        raise HTTPException(404, f"找不到試跑 #{run_id}")
    return run


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    store.delete_run(conn, run_id)
