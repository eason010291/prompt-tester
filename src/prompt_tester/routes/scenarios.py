import base64
import json
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import scenario_revisions as revisions
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
        file_bytes = base64.b64decode(body.data_base64)
        raw = st_card.load_card_bytes(file_bytes, body.filename)
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
    saved = store.save_scenario(conn, scenario)
    if file_bytes.startswith(st_card.PNG_SIGNATURE):
        # 保存原圖，匯出 PNG 時沿用
        with conn:
            conn.execute("INSERT OR REPLACE INTO card_images (scenario_id, filename, png) VALUES (?, ?, ?)",
                         (saved.id, body.filename, file_bytes))
    return CardImportResult(scenario=saved, notes=notes)


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


# ---- 修訂紀錄（線性版本） ----

def _scenario_or_404(conn: sqlite3.Connection, sid: int) -> Scenario:
    s = store.get_scenario(conn, sid)
    if s is None:
        raise HTTPException(404, f"找不到情境 #{sid}")
    return s


@router.get("/{sid}/revisions")
def list_revisions(sid: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """版本列表（新的在前）與目前內容是否已存成版本。"""
    s = _scenario_or_404(conn, sid)
    revs = revisions.list_revisions(conn, sid)
    return {"revisions": revs, "dirty": revisions.is_dirty(conn, s), "latest": revs[0]["id"] if revs else None}


class RevisionIn(BaseModel):
    message: str = ""


@router.post("/{sid}/revisions")
def create_revision(sid: int, body: RevisionIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rev, created = revisions.create(conn, _scenario_or_404(conn, sid), message=body.message)
    if not created:
        raise HTTPException(422, "內容和最新版本相同，不需要再存一版")
    return rev


@router.get("/{sid}/revisions/{rid}")
def get_revision(sid: int, rid: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rev = revisions.get(conn, rid)
    if rev is None or rev["scenario_id"] != sid:
        raise HTTPException(404, f"找不到版本 #{rid}")
    return rev


@router.get("/{sid}/diff")
def diff_revisions(sid: int, a: int, b: str = "CURRENT", conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """a：版本 id；b：版本 id 或 CURRENT（目前內容）。"""
    current = _scenario_or_404(conn, sid)
    ra = revisions.get(conn, a)
    if ra is None or ra["scenario_id"] != sid:
        raise HTTPException(404, f"找不到版本 #{a}")
    if b == "CURRENT":
        new = current
    else:
        rb = revisions.get(conn, int(b))
        if rb is None or rb["scenario_id"] != sid:
            raise HTTPException(404, f"找不到版本 #{b}")
        new = revisions.scenario_of(rb, sid)
    return revisions.diff(revisions.scenario_of(ra, sid), new)


@router.post("/{sid}/revisions/{rid}/restore")
def restore_revision(sid: int, rid: int, conn: sqlite3.Connection = Depends(get_db)) -> Scenario:
    try:
        return revisions.restore(conn, sid, rid)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e


# ---- 匯出角色卡（ST 格式） ----

def _download_name(name: str, ext: str) -> str:
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "character"
    return f"attachment; filename=\"card.{ext}\"; filename*=UTF-8''{quote(safe + '.' + ext)}"


@router.get("/{sid}/export")
def export_card(sid: int, format: str = "json", spec: str = "v3", conn: sqlite3.Connection = Depends(get_db)) -> Response:
    """匯出角色卡，格式與 SillyTavern 1.18.0 相同。

    - json：spec=v3（chara_card_v3，預設）或 v2；頂層保留 V1 欄位，完整資料在 data。
    - png：沿用匯入時的原圖（沒有原圖時用純色預設圖），寫入 chara（V2）與 ccv3（V3）兩個 tEXt 區塊。
    """
    s = _scenario_or_404(conn, sid)
    if s.card is None:
        raise HTTPException(422, "這個情境沒有角色卡（手動建立的情境無法匯出成角色卡）")
    name = s.card.name or s.char
    if format == "json":
        card = st_card.export_card(s, "chara_card_v3" if spec == "v3" else "chara_card_v2")
        return Response(json.dumps(card, ensure_ascii=False, indent=4), media_type="application/json",
                        headers={"Content-Disposition": _download_name(name, "json")})
    if format == "png":
        row = conn.execute("SELECT png FROM card_images WHERE scenario_id = ?", (sid,)).fetchone()
        image = row["png"] if row else st_card.placeholder_png()
        png = st_card.write_png_card(image, st_card.export_card(s, "chara_card_v2"), st_card.export_card(s, "chara_card_v3"))
        return Response(png, media_type="image/png", headers={
            "Content-Disposition": _download_name(name, "png"),
            "X-Card-Image": "original" if row else "placeholder",
        })
    raise HTTPException(422, "format 必須是 json 或 png")


@router.get("/{sid}/card-image")
def card_image(sid: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = conn.execute("SELECT filename FROM card_images WHERE scenario_id = ?", (sid,)).fetchone()
    return {"has_image": row is not None, "filename": row["filename"] if row else None}
