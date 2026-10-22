"""修改者 LLM：設定、內建迴圈（每一輪的紀錄）、③ REST 工具（每個工具一個 endpoint，出現在 OpenAPI）。

工具 endpoint 與 /mcp 共用存取檢查：沒設定 [agent] token 時只接受本機連線。
"""

import inspect
import sqlite3
from typing import Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .. import agent_tools as at
from .. import modifier
from ..mcp_server import access_error, client_name
from .deps import get_db

router = APIRouter(prefix="/api/agent")


def _modifier(request: Request) -> modifier.Manager:
    return request.app.state.modifier


# ---- 設定 ----

@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_db)) -> at.AgentSettings:
    return at.load_settings(conn)


@router.put("/settings")
def put_settings(body: at.AgentSettings, conn: sqlite3.Connection = Depends(get_db)) -> at.AgentSettings:
    # 歷史由伺服器維護，不接受前端覆寫
    body.prompt_history = at.load_settings(conn).prompt_history
    return at.save_settings(conn, body)


@router.get("/default-prompt")
def default_prompt() -> dict:
    return {"text": at.DEFAULT_SYSTEM_PROMPT}


# ---- 內建迴圈 ----

class RoundIn(BaseModel):
    instructions: str = ""
    experiment_id: int | None = None
    # 提交後用新版本重跑來源實驗
    auto_rerun: bool = False
    # 省略時用設定頁的修改者連線
    connection_id: int | None = None
    # 修改對象：preset（預設）或 card（情境的角色卡，需 scenario_id）；一輪只改一個對象
    target: Literal["preset", "card"] = "preset"
    scenario_id: int | None = None


@router.post("/rounds")
async def start_round(body: RoundIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        rid = _modifier(request).start(instructions=body.instructions, experiment_id=body.experiment_id,
                                       auto_rerun=body.auto_rerun, connection_id=body.connection_id,
                                       target=body.target, scenario_id=body.scenario_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return modifier.get_round(conn, rid)


@router.get("/rounds")
async def list_rounds(experiment_id: int | None = None, conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return modifier.list_rounds(conn, experiment_id)


@router.get("/rounds/{rid}")
async def get_round(rid: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    r = modifier.get_round(conn, rid)
    if r is None:
        raise HTTPException(404, f"找不到修改紀錄 #{rid}")
    return r


@router.post("/rounds/{rid}/cancel")
async def cancel_round(rid: int, request: Request) -> dict:
    return {"ok": _modifier(request).cancel(rid)}


@router.post("/rounds/{rid}/discard")
async def discard_round(rid: int, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if _modifier(request).running() == rid:
        raise HTTPException(422, "這輪還在進行中，請先中止")
    try:
        modifier.discard(conn, rid)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return modifier.get_round(conn, rid)


# ---- ③ REST 工具 ----

def check_access(request: Request) -> None:
    err = access_error(request, request.app.state.settings.agent_token)
    if err:
        raise HTTPException(403, err)


@router.get("/tools", dependencies=[Depends(check_access)])
def list_tools(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    settings = at.load_settings(conn)
    return [
        {"name": t.name, "description": t.description, "input_schema": t.schema(), "builtin": t.builtin,
         "enabled": not t.gate or getattr(settings, t.gate)}
        for t in at.REGISTRY.values()
    ]


def _endpoint(t: at.Tool):
    async def endpoint(request: Request, body=None):
        check_access(request)
        conn = request.app.state.db
        name = client_name(request, default="rest")
        ctx = at.Ctx(conn=conn, author=f"agent:{name}", settings=at.load_settings(conn),
                     manager=request.app.state.experiments, meta={"source": "rest", "client": name}, branch_prefix="agent")
        try:
            result = at.call(ctx, t.name, body.model_dump() if body is not None else {})
        except at.ToolError as e:
            raise HTTPException(422, str(e)) from e
        return {"result": result}

    # 讓 FastAPI 依參數模型產生 OpenAPI 文件
    endpoint.__signature__ = inspect.Signature([
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        inspect.Parameter("body", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=t.args | None,
                          default=Body(None)),
    ])
    endpoint.__name__ = f"tool_{t.name}"
    return endpoint


def register_tools(app: FastAPI) -> None:
    for t in at.REGISTRY.values():
        app.add_api_route(f"/api/agent/tools/{t.name}", _endpoint(t), methods=["POST"], summary=t.description,
                          tags=["agent tools"])
