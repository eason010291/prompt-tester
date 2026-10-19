"""M1：工作區（編輯中的預設）與組裝預覽。"""

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..build import build
from ..macros import SUPPORTED_MACROS
from ..preset import Preset, PreviewContext, starter_context, starter_preset

router = APIRouter(prefix="/api")

WORKSPACE_ID = 1


def get_db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


class Workspace(BaseModel):
    preset: Preset
    context: PreviewContext
    updated_at: str | None = None


@router.get("/workspace")
def get_workspace(conn: sqlite3.Connection = Depends(get_db)) -> Workspace:
    row = conn.execute("SELECT preset, context, updated_at FROM workspaces WHERE id = ?", (WORKSPACE_ID,)).fetchone()
    if row is None:
        return Workspace(preset=starter_preset(), context=starter_context())
    return Workspace(
        preset=Preset.model_validate_json(row["preset"]),
        context=PreviewContext.model_validate_json(row["context"]),
        updated_at=row["updated_at"],
    )


@router.put("/workspace")
def put_workspace(ws: Workspace, conn: sqlite3.Connection = Depends(get_db)) -> Workspace:
    ws.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """
            INSERT INTO workspaces (id, preset, context, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET preset = excluded.preset, context = excluded.context,
                                          updated_at = excluded.updated_at
            """,
            (WORKSPACE_ID, ws.preset.model_dump_json(), ws.context.model_dump_json(), ws.updated_at),
        )
    return ws


class BuildRequest(BaseModel):
    preset: Preset
    context: PreviewContext
    expand_macros: bool = True


class SourceOut(BaseModel):
    origin: str
    block_id: str | None
    name: str


class MessageOut(BaseModel):
    role: str
    content: str
    name: str | None
    tokens: int
    sources: list[SourceOut]


class BuildResponse(BaseModel):
    messages: list[MessageOut]
    total_tokens: int
    budget: int
    history_total: int
    history_included: int
    block_tokens: dict[str, int]
    variables: dict[str, str]
    warnings: list[str]


@router.post("/build")
def build_preview(req: BuildRequest) -> BuildResponse:
    ctx = req.context
    result = build(
        req.preset,
        ctx.history,
        {"char": ctx.char, "user": ctx.user},
        seed=ctx.seed,
        expand_macros=req.expand_macros,
    )

    return BuildResponse(
        messages=[
            MessageOut(
                role=m.role,
                content=m.content,
                name=m.name,
                tokens=m.tokens,
                sources=[SourceOut(origin=s.origin, block_id=s.block_id, name=s.name) for s in m.sources],
            )
            for m in result.messages
        ],
        total_tokens=result.total_tokens,
        budget=result.budget,
        history_total=result.history_total,
        history_included=result.history_included,
        block_tokens=result.block_tokens,
        variables=result.variables,
        warnings=result.warnings,
    )


@router.get("/macros")
def list_macros() -> list[str]:
    return SUPPORTED_MACROS
