"""腳本（繼承樹）與金字塔實驗。

實驗相關的 endpoint 用 async：與背景執行的階段在同一個事件迴圈執行緒存取 SQLite，也才能建立背景工作。
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import experiments as ex
from .. import scripts as sc
from .. import store
from ..preset import Preset
from ..scripts import Script, ScriptData
from .deps import get_db

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


@router.post("/experiments/{exp_id}/advance")
async def advance(exp_id: int, body: AdvanceIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        _manager(request).advance(exp_id, body.run_ids)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return ex.detail(conn, exp_id)


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
