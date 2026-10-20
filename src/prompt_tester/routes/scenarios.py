import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import store
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
