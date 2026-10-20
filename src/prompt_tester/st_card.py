"""SillyTavern 角色卡（V1 / V2 / V3）讀取。

- JSON：允許 UTF-8 BOM。
- PNG：讀 tEXt 區塊，`ccv3` 優先於 `chara`（ST src/character-card-parser.js:50-70），內容為 base64 的 UTF-8 JSON。
- 三種版本統一成 CharacterCard；原始 JSON 存在 raw，供日後匯出。
世界書條目的轉換在 worldinfo.py。
"""

import base64
import json
import struct
import zlib

from .entities import CharacterCard, DepthPrompt
from .worldinfo import lorebook_from_card

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class CardFormatError(ValueError):
    pass


def read_png_text_chunks(data: bytes) -> dict[str, str]:
    if not data.startswith(PNG_SIGNATURE):
        raise CardFormatError("不是 PNG 檔")
    chunks: dict[str, str] = {}
    i = len(PNG_SIGNATURE)
    while i + 8 <= len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        ctype = data[i + 4 : i + 8]
        body = data[i + 8 : i + 8 + length]
        if ctype == b"tEXt" and b"\0" in body:
            key, text = body.split(b"\0", 1)
            chunks[key.decode("latin-1").lower()] = text.decode("latin-1")
        elif ctype == b"zTXt" and b"\0" in body:
            key, rest = body.split(b"\0", 1)
            chunks[key.decode("latin-1").lower()] = zlib.decompress(rest[1:]).decode("latin-1")
        elif ctype == b"IEND":
            break
        i += 12 + length
    return chunks


def load_card_bytes(data: bytes, filename: str = "") -> dict:
    if data.startswith(PNG_SIGNATURE):
        chunks = read_png_text_chunks(data)
        encoded = chunks.get("ccv3") or chunks.get("chara")
        if not encoded:
            raise CardFormatError("PNG 裡沒有角色卡資料（找不到 ccv3 / chara 區塊）")
        try:
            return json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise CardFormatError(f"PNG 角色卡資料無法解析：{e}") from e
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise CardFormatError(f"JSON 無法解析：{e}") from e


def parse_card(raw: dict) -> tuple[CharacterCard, list[str]]:
    if not isinstance(raw, dict):
        raise CardFormatError("角色卡必須是 JSON 物件")
    notes: list[str] = []
    spec = raw.get("spec")
    if spec in ("chara_card_v2", "chara_card_v3"):
        data = raw.get("data") or {}
    elif "name" in raw and ("first_mes" in raw or "description" in raw):
        data = raw  # V1：欄位在頂層
        notes.append("V1 角色卡（沒有 spec 欄位）。")
    else:
        raise CardFormatError("不是 SillyTavern 角色卡（缺少 spec 或 name / first_mes）")

    ext = data.get("extensions") or {}
    dp = ext.get("depth_prompt") or {}
    book_raw = data.get("character_book")
    book, book_notes = lorebook_from_card(book_raw) if book_raw else (None, [])
    notes += book_notes

    card = CharacterCard(
        spec=spec or "v1",
        name=data.get("name") or "",
        description=data.get("description") or "",
        personality=data.get("personality") or "",
        scenario=data.get("scenario") or "",
        first_mes=data.get("first_mes") or "",
        mes_example=data.get("mes_example") or "",
        creator_notes=data.get("creator_notes") or "",
        system_prompt=data.get("system_prompt") or "",
        post_history_instructions=data.get("post_history_instructions") or "",
        alternate_greetings=[g for g in data.get("alternate_greetings") or [] if isinstance(g, str)],
        depth_prompt=DepthPrompt(
            prompt=dp.get("prompt") or "",
            depth=int(dp.get("depth", 4)),
            role=dp.get("role") if dp.get("role") in ("system", "user", "assistant") else "system",
        ),
        regex_scripts=ext.get("regex_scripts") or [],
        book=book,
        raw=raw,
    )

    if ext.get("tavern_helper"):
        notes.append("角色卡含 Tavern Helper 腳本，本工具不執行（開場白中的互動元件不會運作）。")
    if card.regex_scripts:
        notes.append(f"{len(card.regex_scripts)} 個角色正則已保留（M8 才會用於渲染）。")
    return card, notes
