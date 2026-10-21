"""腳本（繼承樹）與金字塔實驗。

實驗相關的 endpoint 用 async：與背景執行的階段在同一個事件迴圈執行緒存取 SQLite，也才能建立背景工作。
"""

import sqlite3
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import experiments as ex
from .. import scripts as sc
from .. import store, workspace
from ..preset import Preset
from ..scripts import Script, ScriptData
from .deps import get_db, get_http

router = APIRouter(prefix="/api")


def _manager(request: Request) -> ex.Manager:
    return request.app.state.experiments


# ---- 腳本 ----

def _script_out(conn: sqlite3.Connection, s: Script) -> dict:
    try:
        data, warnings = sc.resolve(conn, s.id)
        resolved, error = data.model_dump(), None
    except ValueError as e:
        resolved, warnings, error = sc.resolve_raw(conn, s.id)[0], [], str(e)
    return s.model_dump() | {"resolved": resolved, "warnings": warnings, "error": error}


@router.get("/scripts")
def list_scripts(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return [_script_out(conn, s) for s in sc.list_all(conn)]


class ScriptIn(BaseModel):
    name: str
    parent_id: int | None = None
    notes: str = ""
    # 完整數值；伺服器依父腳本算出覆寫。省略時沿用父腳本（沒有覆寫）
    data: ScriptData | None = None
    # 要還原繼承的覆寫路徑
    reset: list[str] = []


def _save(conn: sqlite3.Connection, script: Script, body: ScriptIn) -> dict:
    script.name, script.parent_id, script.notes = body.name, body.parent_id, body.notes
    try:
        if body.reset:
            for p in body.reset:
                script.overrides.pop(p, None)
            saved = sc.save(conn, script)
        else:
            saved = sc.save(conn, script, body.data)
    except (KeyError, ValueError) as e:
        raise HTTPException(422, str(e).strip("'")) from e
    return _script_out(conn, saved)


@router.post("/scripts")
def create_script(body: ScriptIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return _save(conn, Script(), body)


@router.get("/scripts/{script_id}")
def get_script(script_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    s = sc.get(conn, script_id)
    if s is None:
        raise HTTPException(404, f"找不到腳本 #{script_id}")
    return _script_out(conn, s)


@router.put("/scripts/{script_id}")
def update_script(script_id: int, body: ScriptIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    s = sc.get(conn, script_id)
    if s is None:
        raise HTTPException(404, f"找不到腳本 #{script_id}")
    return _save(conn, s, body)


@router.delete("/scripts/{script_id}", status_code=204)
def delete_script(script_id: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    try:
        sc.delete(conn, script_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# ---- 評審設定 ----

@router.get("/judge")
def get_judge(conn: sqlite3.Connection = Depends(get_db)) -> ex.JudgeSettings:
    return ex.load_judge(conn)


@router.put("/judge")
def put_judge(body: ex.JudgeSettings, conn: sqlite3.Connection = Depends(get_db)) -> ex.JudgeSettings:
    store.set_setting(conn, ex.JUDGE_KEY, body.model_dump())
    return body


# ---- 實驗 ----

class ExperimentIn(BaseModel):
    name: str = ""
    preset: Preset
    script_id: int | None = None
    scenario_ids: list[int] = []
    connection_id: int | None = None
    # 用同一份腳本快照與情境重跑某個實驗（迴歸）；其餘欄位沿用原實驗
    rerun_of: int | None = None


@router.post("/experiments")
async def create_experiment(body: ExperimentIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        if body.rerun_of is not None:
            exp_id = ex.create_rerun(conn, _manager(request), body.rerun_of, body.preset, name=body.name,
                                     connection_id=body.connection_id)
        else:
            if body.script_id is None or body.connection_id is None:
                raise HTTPException(422, "請選擇腳本與連線")
            exp_id = ex.create_and_start(conn, _manager(request), name=body.name, preset=body.preset,
                                         script_id=body.script_id, scenario_ids=body.scenario_ids,
                                         connection_id=body.connection_id)
    except (ex.ExperimentError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    return ex.detail(conn, exp_id)


@router.get("/experiments")
async def list_experiments(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return ex.list_experiments(conn)


@router.get("/experiments/{exp_id}")
async def get_experiment(exp_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    d = ex.detail(conn, exp_id)
    if d is None:
        raise HTTPException(404, f"找不到實驗 #{exp_id}")
    return d


class AdvanceIn(BaseModel):
    run_ids: list[int] = []
    # 使用者接管輸入：下一階段新回合的使用者發言 {全域回合: 內容}；空白的仍由 LLM 扮演
    user_turns: dict[int, str] = {}


@router.post("/experiments/{exp_id}/advance")
async def advance(exp_id: int, body: AdvanceIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        _manager(request).advance(exp_id, body.run_ids, body.user_turns)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return ex.detail(conn, exp_id)


class RetryTarget(BaseModel):
    run_id: int
    # message：單則（需 turn / role）；branch：整條分支；resume：從中斷處續跑
    kind: Literal["message", "branch", "resume"] = "message"
    turn: int | None = None
    role: Literal["user", "assistant"] | None = None


class StageRetry(BaseModel):
    """階段層級：本階段所有分支。kind=message 時指定回合與角色；branch = 整條分支；resume = 只續跑未完成的。"""

    stage: int
    kind: Literal["message", "branch", "resume"] = "message"
    turn: int | None = None
    role: Literal["user", "assistant"] | None = None


class RetryIn(BaseModel):
    items: list[RetryTarget] = []
    stage: StageRetry | None = None
    # 重試使用者發言時改用這段手動輸入（省略則由模擬使用者重新產生）
    user_text: str | None = None
    # 之後回合的使用者發言：keep 沿用、regenerate 重新產生
    later_users: Literal["keep", "regenerate"] = "regenerate"
    # 重試原因：勾選的標籤與「其他」說明（續跑與連帶重跑由系統標記）
    tags: list[str] = []
    note: str = ""


@router.post("/experiments/{exp_id}/retry")
async def retry(exp_id: int, body: RetryIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    targets = [t.model_dump() for t in body.items]
    scope = None
    if body.stage is not None:
        st = body.stage
        scope = "stage"
        for r in store.list_runs(conn, limit=10_000, experiment_id=exp_id):
            if r["stage"] != st.stage:
                continue
            if st.kind == "resume" and r["status"] not in ("error", "cancelled"):
                continue
            targets.append({"run_id": r["id"], "kind": st.kind, "turn": st.turn, "role": st.role})
    for t in targets:
        if t["kind"] == "message" and (t["turn"] is None or t["role"] is None):
            raise HTTPException(422, "單則重試需要指定 turn 與 role")
    if not targets:
        raise HTTPException(422, "沒有要重試或續跑的分支")
    try:
        result = _manager(request).retry(exp_id, targets, user_text=body.user_text, later_users=body.later_users,
                                         tags=body.tags, note=body.note, scope=scope)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if not result["started"]:
        raise HTTPException(422, "沒有可以重試的分支：" + "；".join(f"#{x['run_id']} {x['reason']}" for x in result["skipped"]))
    return result


# ---- 重試原因與統計 ----

class RetryReasons(BaseModel):
    reasons: list[str]


@router.get("/retry-reasons")
def get_retry_reasons(conn: sqlite3.Connection = Depends(get_db)) -> RetryReasons:
    return RetryReasons(reasons=ex.load_retry_reasons(conn))


@router.put("/retry-reasons")
def put_retry_reasons(body: RetryReasons, conn: sqlite3.Connection = Depends(get_db)) -> RetryReasons:
    reasons = [r.strip() for r in body.reasons if r.strip()]
    store.set_setting(conn, ex.RETRY_REASONS_KEY, {"reasons": reasons})
    return RetryReasons(reasons=reasons or ex.DEFAULT_RETRY_REASONS)


@router.get("/retry-stats")
async def retry_stats_all(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ex.retry_stats(conn)


@router.get("/experiments/{exp_id}/retry-stats")
async def retry_stats(exp_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ex.retry_stats(conn, exp_id)


# ---- 迴歸試算與驗證輪次 ----

@router.get("/experiments/{exp_id}/diff-workspace")
async def diff_workspace(exp_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """實驗使用的預設 → 目前工作區。"""
    try:
        return ex.diff_workspace(conn, exp_id, workspace.working(conn))
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e


class ProbeIn(BaseModel):
    run_id: int
    turn: int
    # 省略時用目前工作區
    preset: Preset | None = None


@router.post("/experiments/{exp_id}/probe")
async def probe(exp_id: int, body: ProbeIn, request: Request, conn: sqlite3.Connection = Depends(get_db),
                http: httpx.AsyncClient = Depends(get_http)) -> StreamingResponse:
    preset = body.preset or workspace.working(conn)
    gen = ex.probe(conn, http, exp_id, body.run_id, body.turn, preset)
    try:
        first = await gen.__anext__()  # 先驗證參數，錯誤以 HTTP 狀態回報
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    async def stream():
        yield first
        async for chunk in gen:
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/experiments/{exp_id}/probes")
async def probes(exp_id: int, run_id: int, turn: int, conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return ex.list_probes(conn, run_id, turn)


class VerifyIn(BaseModel):
    from_stage: int
    # 省略時驗證整個階段
    run_ids: list[int] = []
    preset: Preset | None = None
    name: str = ""


@router.post("/experiments/{exp_id}/verify")
async def verify(exp_id: int, body: VerifyIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """開啟驗證輪次：用新版本只重跑指定階段的分支（沿用原對話與使用者發言）。"""
    try:
        new_id = ex.create_verify(conn, _manager(request), exp_id, body.preset or workspace.working(conn),
                                  from_stage=body.from_stage, run_ids=body.run_ids, name=body.name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return ex.detail(conn, new_id)


@router.post("/experiments/{exp_id}/runs/{run_id}/cancel")
async def cancel_run(exp_id: int, run_id: int, request: Request) -> dict:
    """中止單一分支（卡住時用）；其他分支繼續。"""
    try:
        return {"status": _manager(request).cancel_run(exp_id, run_id)}
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/experiments/{exp_id}/cancel")
async def cancel(exp_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    _manager(request).cancel(exp_id)
    return {"ok": True}


class JudgeIn(BaseModel):
    stage: int


@router.post("/experiments/{exp_id}/judge")
async def judge(exp_id: int, body: JudgeIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if ex.get_raw(conn, exp_id) is None:
        raise HTTPException(404, f"找不到實驗 #{exp_id}")
    try:
        return await _manager(request).judge(exp_id, body.stage)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.delete("/experiments/{exp_id}", status_code=204)
async def delete_experiment(exp_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> None:
    if _manager(request).running(exp_id):
        raise HTTPException(422, "實驗還在執行中，請先中止")
    ex.delete(conn, exp_id)
