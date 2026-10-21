"""評語、評語模板、兩次試跑的比較。"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import feedback as fb
from .. import store, vcs
from ..preset import Preset
from .deps import get_db

router = APIRouter(prefix="/api")

TEMPLATE_KEY = "feedback_template"


class FeedbackIn(BaseModel):
    target_type: str
    target_id: int
    text: str


class FeedbackText(BaseModel):
    text: str


@router.get("/feedback")
def list_feedback(
    target_type: str | None = None,
    target_id: int | None = None,
    run_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return fb.list_for(conn, target_type=target_type, target_id=target_id, run_id=run_id)


@router.post("/feedback")
def add_feedback(body: FeedbackIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        return fb.add(conn, body.target_type, body.target_id, body.text)
    except (KeyError, ValueError) as e:
        raise HTTPException(422, str(e).strip("'")) from e


@router.put("/feedback/{fid}")
def update_feedback(fid: int, body: FeedbackText, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        return fb.update(conn, fid, body.text)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e


@router.delete("/feedback/{fid}", status_code=204)
def delete_feedback(fid: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    fb.delete(conn, fid)


@router.post("/feedback/parse")
def parse(body: FeedbackText) -> dict[str, float]:
    return fb.parse_scores(body.text)


class Template(BaseModel):
    text: str = fb.DEFAULT_TEMPLATE


@router.get("/feedback-template")
def get_template(conn: sqlite3.Connection = Depends(get_db)) -> Template:
    return Template.model_validate(store.get_setting(conn, TEMPLATE_KEY) or {})


@router.put("/feedback-template")
def put_template(body: Template, conn: sqlite3.Connection = Depends(get_db)) -> Template:
    store.set_setting(conn, TEMPLATE_KEY, body.model_dump())
    return body


def _snapshot(conn: sqlite3.Connection, preset_hash: str) -> Preset | None:
    row = conn.execute("SELECT data FROM preset_snapshots WHERE hash = ?", (preset_hash,)).fetchone()
    return Preset.model_validate_json(row["data"]) if row else None


@router.get("/compare/runs")
def compare_runs(a: int, b: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """兩次試跑並排：逐回合訊息、兩份預設的區塊 diff（a → b）、各自的評語與分數。"""
    runs = {}
    for key, rid in (("a", a), ("b", b)):
        run = store.get_run(conn, rid)
        if run is None:
            raise HTTPException(404, f"找不到試跑 #{rid}")
        run["feedback"] = fb.list_for(conn, run_id=rid)
        run["scores"] = fb.aggregate(run["feedback"])
        runs[key] = run
    hashes = [runs[k]["preset_hash"] for k in ("a", "b")]
    pa, pb = (_snapshot(conn, h) for h in hashes)
    # 前端用各自的預設套正則渲染
    runs["a"]["preset"], runs["b"]["preset"] = pa, pb
    return {
        **runs,
        "same_preset": hashes[0] == hashes[1],
        "diff": vcs.diff_presets(pa, pb) if pa and pb else None,
    }
