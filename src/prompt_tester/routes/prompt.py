"""工作區（編輯中的預設）、組裝預覽、ST 預設匯入 / 匯出。"""

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import st_preset, store, tokens
from ..build import BuildResult, build
from ..entities import Scenario
from ..macros import SUPPORTED_MACROS
from ..preset import LegacyPreviewContext, Preset, PreviewContext, starter_preset
from .deps import get_db

router = APIRouter(prefix="/api")

WORKSPACE_ID = 1


class Workspace(BaseModel):
    preset: Preset
    context: PreviewContext
    updated_at: str | None = None


def migrate_workspace(conn: sqlite3.Connection) -> None:
    """啟動時執行：M1 工作區的內嵌情境轉成正式情境；沒有任何情境時建立範例情境。"""
    row = conn.execute("SELECT context FROM workspaces WHERE id = ?", (WORKSPACE_ID,)).fetchone()
    if row is not None:
        raw_ctx = json.loads(row["context"])
        if "scenario_id" not in raw_ctx:
            legacy = LegacyPreviewContext.model_validate(raw_ctx)
            scenario = store.save_scenario(conn, store.scenario_from_legacy(legacy))
            context = _context_for(scenario, seed=legacy.seed)
            with conn:
                conn.execute("UPDATE workspaces SET context = ? WHERE id = ?", (context.model_dump_json(), WORKSPACE_ID))
    if not store.list_scenarios(conn):
        store.save_scenario(conn, store.starter_scenario())


@router.get("/workspace")
def get_workspace(conn: sqlite3.Connection = Depends(get_db)) -> Workspace:
    row = conn.execute("SELECT preset, context, updated_at FROM workspaces WHERE id = ?", (WORKSPACE_ID,)).fetchone()
    if row is None:
        return Workspace(preset=starter_preset(), context=_context_for(_first_scenario(conn)))
    return Workspace(
        preset=Preset.model_validate_json(row["preset"]),
        context=PreviewContext.model_validate_json(row["context"]),
        updated_at=row["updated_at"],
    )


def _first_scenario(conn: sqlite3.Connection) -> Scenario:
    existing = store.list_scenarios(conn)
    return existing[0] if existing else store.save_scenario(conn, store.starter_scenario())


def _context_for(scenario: Scenario, seed: int = 1) -> PreviewContext:
    return PreviewContext(scenario_id=scenario.id, seed=seed, user_input=scenario.user_turns[0] if scenario.user_turns else "")


@router.put("/workspace")
def put_workspace(ws: Workspace, conn: sqlite3.Connection = Depends(get_db)) -> Workspace:
    ws.updated_at = store.now_iso()
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


def resolve_scenario(conn: sqlite3.Connection, context: PreviewContext) -> Scenario:
    if context.scenario_id is None:
        return Scenario(name="（未選情境）")
    scenario = store.get_scenario(conn, context.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"找不到情境 #{context.scenario_id}")
    return scenario


def build_for(preset: Preset, scenario: Scenario, context: PreviewContext, expand_macros: bool = True) -> BuildResult:
    return build(
        preset,
        scenario.chat(context.user_input),
        scenario.names(),
        seed=context.seed,
        expand_macros=expand_macros,
        card=scenario.card,
        persona=scenario.persona,
    )


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


class WIEntryOut(BaseModel):
    uid: int
    comment: str
    position: int
    depth: int
    order: int
    reason: str
    tokens: int


class WorldInfoOut(BaseModel):
    budget: int
    used_tokens: int
    activated: list[WIEntryOut]
    total_entries: int


class BuildResponse(BaseModel):
    messages: list[MessageOut]
    total_tokens: int
    budget: int
    history_total: int
    history_included: int
    block_tokens: dict[str, int]
    variables: dict[str, str]
    warnings: list[str]
    worldinfo: WorldInfoOut | None = None


@router.post("/build")
def build_preview(req: BuildRequest, conn: sqlite3.Connection = Depends(get_db)) -> BuildResponse:
    scenario = resolve_scenario(conn, req.context)
    result = build_for(req.preset, scenario, req.context, req.expand_macros)
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
        worldinfo=_wi_out(result, scenario),
    )


def _wi_out(result: BuildResult, scenario: Scenario) -> WorldInfoOut | None:
    wi = result.worldinfo
    if wi is None:
        return None
    return WorldInfoOut(
        budget=wi.budget,
        used_tokens=wi.used_tokens,
        total_entries=len(scenario.card.book.entries) if scenario.card and scenario.card.book else 0,
        activated=[
            WIEntryOut(
                uid=a.entry.uid,
                comment=a.entry.comment,
                position=a.entry.position,
                depth=a.entry.depth,
                order=a.entry.order,
                reason=a.reason,
                tokens=tokens.count(a.content),
            )
            for a in wi.activated
        ],
    )


@router.get("/macros")
def list_macros() -> list[str]:
    return SUPPORTED_MACROS


class ImportRequest(BaseModel):
    data: dict
    filename: str = ""


class ImportResponse(BaseModel):
    preset: Preset
    notes: list[str]


@router.post("/import/st-preset")
def import_st(req: ImportRequest) -> ImportResponse:
    name = req.filename.rsplit(".", 1)[0] if req.filename else ""
    try:
        preset, notes = st_preset.import_preset(req.data, name)
    except st_preset.PresetFormatError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return ImportResponse(preset=preset, notes=notes)


@router.post("/export/st-preset")
def export_st(preset: Preset) -> dict:
    return st_preset.export_preset(preset)
