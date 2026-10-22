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


# ---- 匯出（對照 ST 1.18.0 src/endpoints/characters.js charaFormatData / convertWorldInfoToCharacterBook、
#      src/character-card-parser.js write）----


def _lore_entry_out(e, raw_entry: dict | None, index: int) -> dict:
    """LoreEntry → ST character_book 條目；原始條目中本工具不認得的欄位原樣保留。"""
    from .worldinfo import MATCH_SOURCES, POS_BEFORE

    base = json.loads(json.dumps(raw_entry)) if raw_entry else {}
    ext = dict(base.get("extensions") or {})
    sources = set(e.match_sources)
    dur = e.delay_until_recursion
    ext.update({
        "position": e.position,
        "exclude_recursion": e.exclude_recursion,
        "display_index": ext.get("display_index", index),
        "probability": e.probability,
        "useProbability": e.use_probability,
        "depth": e.depth,
        "selectiveLogic": e.selective_logic,
        "outlet_name": e.outlet_name,
        "group": e.group,
        "group_override": e.group_override,
        "group_weight": e.group_weight,
        "prevent_recursion": e.prevent_recursion,
        # ST：true 或遞迴層級數字；0 = 關閉
        "delay_until_recursion": (dur if dur > 1 else bool(dur)),
        "scan_depth": e.scan_depth,
        "match_whole_words": e.match_whole_words,
        "use_group_scoring": e.use_group_scoring,  # None = 沿用全域設定（ST 匯入時 ?? null）
        "case_sensitive": e.case_sensitive,
        "automation_id": ext.get("automation_id", ""),
        "role": e.role,
        "vectorized": ext.get("vectorized", False),
        "sticky": ext.get("sticky"),
        "cooldown": ext.get("cooldown"),
        "delay": e.delay,
        "triggers": e.triggers,
        "ignore_budget": e.ignore_budget,
        **{flag: src in sources for flag, src in MATCH_SOURCES.items()},
    })
    base.update({
        "id": e.uid,
        "keys": e.keys,
        "secondary_keys": e.secondary_keys,
        "comment": e.comment,
        "content": e.content,
        "constant": e.constant,
        "selective": e.selective,
        "insertion_order": e.order,
        "enabled": e.enabled,
        "position": "before_char" if e.position == POS_BEFORE else "after_char",
        "use_regex": base.get("use_regex", True),
        "extensions": ext,
    })
    return base


def _book_out(book, raw_book: dict | None) -> dict:
    raw_book = raw_book or {}
    raw_entries = raw_book.get("entries") or []
    by_id = {}
    for i, re_ in enumerate(raw_entries):
        if isinstance(re_, dict):
            by_id[re_["id"] if isinstance(re_.get("id"), int) else i] = re_
    out = json.loads(json.dumps({k: v for k, v in raw_book.items() if k != "entries"}))
    out.update({"name": book.name or raw_book.get("name") or "", "entries": [
        _lore_entry_out(e, by_id.get(e.uid), i) for i, e in enumerate(book.entries)
    ]})
    for key in ("scan_depth", "token_budget", "recursive_scanning"):
        value = getattr(book, key)
        if value is not None:
            out[key] = value
    return out


def export_card(scenario, spec: str = "chara_card_v3") -> dict:
    """情境的角色卡 → ST 格式的角色卡物件（V2 或 V3）。

    結構與 ST 匯出相同：頂層保留 V1 欄位（name、description…、creatorcomment、talkativeness、fav、tags），
    完整資料在 data（含 extensions.depth_prompt、regex_scripts、world 與 character_book）。
    原始卡片中本工具不認得的欄位（tavern_helper 等擴充、create_date…）原樣保留。
    """
    card = scenario.card
    if card is None:
        raise CardFormatError("這個情境沒有角色卡")
    raw = json.loads(json.dumps(card.raw or {}))
    raw.pop("json_data", None)
    raw.pop("chat", None)  # ST unsetPrivateFields
    raw_data = raw.get("data") if isinstance(raw.get("data"), dict) else ({} if raw.get("spec") else dict(raw))
    data = json.loads(json.dumps(raw_data))
    ext = dict(data.get("extensions") or {})

    # 情境的開場白若只是切換到替代開場白，first_mes 維持原卡；若是改寫過的內容，就寫回 first_mes
    opening = scenario.opening
    first_mes = card.first_mes if (opening == card.first_mes or opening in card.alternate_greetings) else opening
    name = card.name or scenario.char
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []

    ext.update({
        "talkativeness": ext.get("talkativeness", 0.5),
        "fav": False,
        "world": ext.get("world", card.book.name if card.book else ""),
        "depth_prompt": {"prompt": card.depth_prompt.prompt, "depth": card.depth_prompt.depth, "role": card.depth_prompt.role},
    })
    if card.regex_scripts or "regex_scripts" in ext:
        ext["regex_scripts"] = card.regex_scripts
    data.update({
        "name": name,
        "description": card.description,
        "personality": card.personality,
        "scenario": card.scenario,
        "first_mes": first_mes,
        "mes_example": card.mes_example,
        "creator_notes": card.creator_notes,
        "system_prompt": card.system_prompt,
        "post_history_instructions": card.post_history_instructions,
        "tags": tags,
        "creator": data.get("creator", ""),
        "character_version": data.get("character_version", ""),
        "alternate_greetings": card.alternate_greetings,
        "extensions": ext,
    })
    if spec == "chara_card_v3":
        data.setdefault("group_only_greetings", [])
    if card.book is not None:
        data["character_book"] = _book_out(card.book, (raw_data or {}).get("character_book"))
    else:
        data.pop("character_book", None)

    out = {k: v for k, v in raw.items() if k not in ("data", "spec", "spec_version")}
    out.update({
        "name": name,
        "description": card.description,
        "personality": card.personality,
        "scenario": card.scenario,
        "first_mes": first_mes,
        "mes_example": card.mes_example,
        "creatorcomment": card.creator_notes,
        "avatar": "none",
        "talkativeness": ext["talkativeness"],
        "fav": False,
        "tags": tags,
        "spec": spec,
        "spec_version": "3.0" if spec == "chara_card_v3" else "2.0",
        "data": data,
    })
    return out


def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def write_png_card(image: bytes, v2: dict, v3: dict) -> bytes:
    """同 ST write()：移除既有的 chara / ccv3 tEXt，在 IEND 前寫入 chara（V2）與 ccv3（V3），內容為 base64 的 UTF-8 JSON。"""
    if not image.startswith(PNG_SIGNATURE):
        raise CardFormatError("不是 PNG 檔")
    out = [PNG_SIGNATURE]
    i = len(PNG_SIGNATURE)
    while i + 8 <= len(image):
        (length,) = struct.unpack(">I", image[i : i + 4])
        ctype = image[i + 4 : i + 8]
        chunk = image[i : i + 12 + length]
        body = image[i + 8 : i + 8 + length]
        i += 12 + length
        if ctype == b"tEXt" and b"\0" in body and body.split(b"\0", 1)[0].decode("latin-1").lower() in ("chara", "ccv3"):
            continue
        if ctype == b"IEND":
            for key, obj in ((b"chara", v2), (b"ccv3", v3)):
                text = base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
                out.append(_png_chunk(b"tEXt", key + b"\0" + text))
            out.append(chunk)
            break
        out.append(chunk)
    return b"".join(out)


def placeholder_png(width: int = 400, height: int = 600, rgb: tuple[int, int, int] = (232, 224, 210)) -> bytes:
    """沒有原始圖片時用的純色 PNG（ST 匯入時需要一張圖）。"""
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height, 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", raw) + _png_chunk(b"IEND", b"")
