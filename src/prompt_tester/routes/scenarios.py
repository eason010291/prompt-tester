import base64
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import st_card, store
from ..entities import Scenario
from .deps import get_db

router = APIRouter(prefix="/api/scenarios")


@router.get("")
def list_scenarios(conn: sqlite3.Connection = Depends(get_db)) -> list[Scenario]:
    return store.list_scenarios(conn)


@router.post("")
def create_scenario(s: Scenario, conn: sqlite3.Connection = Depends(get_db)) -> Scenario:
    s.id = None
    return store.save_scenario(conn, s)


class CardImport(BaseModel):
    filename: str = ""
    data_base64: str


class CardImportResult(BaseModel):
    scenario: Scenario
    notes: list[str]


@router.post("/import-card")
def import_card(body: CardImport, conn: sqlite3.Connection = Depends(get_db)) -> CardImportResult:
    """從 ST 角色卡（JSON / PNG）建立情境：開場白取 first_mes，其餘欄位進 card。"""
    try:
        raw = st_card.load_card_bytes(base64.b64decode(body.data_base64), body.filename)
        card, notes = st_card.parse_card(raw)
    except (st_card.CardFormatError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    scenario = Scenario(
        name=card.name or body.filename or "匯入的角色卡",
        char=card.name or "角色",
        opening=card.first_mes,
        card=card,
        user_turns=[""],
    )
    if card.alternate_greetings:
        notes.append(f"另有 {len(card.alternate_greetings)} 個替代開場白，可在情境頁切換。")
    return CardImportResult(scenario=store.save_scenario(conn, scenario), notes=notes)


@router.get("/{sid}")
def get_scenario(sid: int, conn: sqlite3.Connection = Depends(get_db)) -> Scenario:
    s = store.get_scenario(conn, sid)
    if s is None:
        raise HTTPException(404, f"找不到情境 #{sid}")
    return s


@router.put("/{sid}")
def update_scenario(sid: int, s: Scenario, conn: sqlite3.Connection = Depends(get_db)) -> Scenario:
    s.id = sid
    try:
        return store.save_scenario(conn, s)
    except KeyError:
        raise HTTPException(404, f"找不到情境 #{sid}") from None


@router.delete("/{sid}", status_code=204)
def delete_scenario(sid: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    store.delete_scenario(conn, sid)
