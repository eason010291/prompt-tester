"""SillyTavern Chat Completion 預設的匯入 / 匯出。

匯入規則（依 ST 1.18.0）：
- 區塊順序與開關取自 `prompt_order` 中 character_id 100001 的那份（oa:686-689）；
  沒有時退回 100000，再沒有就用 `prompts` 陣列順序。`prompts[].enabled` 不使用。
- 不在 order 裡的 prompt：ST 不會送出，這裡附加在最後並設為停用，避免資料遺失。
- 名稱形如 `===…===` 且內容為空的 prompt 視為分隔區塊（使用者慣例；組裝結果不變）。
- 所有無法對應的欄位原樣保存：區塊層級存在 Block.st，預設層級存在 Preset.extra，
  匯出時合併回去，讓 ST 能無損重新匯入。
"""

import copy
import re

from .preset import Block, Options, Params, Preset

GLOBAL_ORDER_ID = 100001
DEFAULT_ORDER_ID = 100000

MARKERS = {
    "chatHistory",
    "dialogueExamples",
    "worldInfoBefore",
    "worldInfoAfter",
    "charDescription",
    "charPersonality",
    "scenario",
    "personaDescription",
}

# 頂層欄位 ↔ Params / Options
PARAM_KEYS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
    "openai_max_tokens": "max_tokens",
    "openai_max_context": "max_context",
}
OPTION_KEYS = {
    "squash_system_messages": "squash_system_messages",
    "new_chat_prompt": "new_chat_prompt",
    "new_example_chat_prompt": "new_example_chat_prompt",
    "wi_format": "wi_format",
    "scenario_format": "scenario_format",
    "personality_format": "personality_format",
}

# Block 直接對應的 prompt 欄位；其餘 prompt 欄位放進 Block.st
BLOCK_KEYS = {"identifier", "name", "role", "content", "injection_position", "injection_depth", "injection_order", "marker", "enabled"}

_DIVIDER_NAME = re.compile(r"^\s*=+.*=+\s*$")


class PresetFormatError(ValueError):
    pass


def is_st_preset(data: object) -> bool:
    return isinstance(data, dict) and isinstance(data.get("prompts"), list)


def import_preset(data: dict, name: str = "") -> tuple[Preset, list[str]]:
    """回傳 (預設, 匯入說明)。"""
    if not is_st_preset(data):
        raise PresetFormatError("不是 SillyTavern Chat Completion 預設：缺少 prompts 陣列")

    notes: list[str] = []
    prompts = {p["identifier"]: p for p in data["prompts"] if isinstance(p, dict) and "identifier" in p}

    orders = {o.get("character_id"): o.get("order", []) for o in data.get("prompt_order", []) if isinstance(o, dict)}
    if GLOBAL_ORDER_ID in orders:
        order = orders[GLOBAL_ORDER_ID]
    elif DEFAULT_ORDER_ID in orders:
        order = orders[DEFAULT_ORDER_ID]
        notes.append("找不到 character_id 100001 的 prompt_order，改用 100000。")
    else:
        order = [{"identifier": i, "enabled": True} for i in prompts]
        notes.append("沒有 prompt_order，改用 prompts 陣列順序並全部啟用。")

    blocks: list[Block] = []
    seen: set[str] = set()
    for entry in order:
        ident = entry.get("identifier")
        if ident not in prompts:
            notes.append(f"prompt_order 引用了不存在的 prompt：{ident}（已略過）")
            continue
        if ident in seen:
            continue
        seen.add(ident)
        blocks.append(_to_block(prompts[ident], bool(entry.get("enabled", True))))

    orphans = [p for i, p in prompts.items() if i not in seen]
    for p in orphans:
        blocks.append(_to_block(p, False))
    if orphans:
        notes.append(f"{len(orphans)} 個 prompt 不在 prompt_order 中（ST 不會送出），已附加在最後並停用。")

    params = Params(**{ours: data[st] for st, ours in PARAM_KEYS.items() if st in data})
    options = Options(**{ours: data[st] for st, ours in OPTION_KEYS.items() if st in data})

    extra = {k: copy.deepcopy(v) for k, v in data.items() if k not in PARAM_KEYS and k not in OPTION_KEYS and k != "prompts"}
    # 保留 100001 以外的 prompt_order；100001 由區塊列表重建
    extra["prompt_order"] = [o for o in data.get("prompt_order", []) if o.get("character_id") != GLOBAL_ORDER_ID]

    regex = data.get("extensions", {}).get("regex_scripts") or []
    if regex:
        notes.append(f"{len(regex)} 個正則腳本已保留（M8 才會用於渲染）。")
    unfilled = sorted({b.marker for b in blocks if b.kind == "marker" and b.enabled and b.marker != "chatHistory"})
    if unfilled:
        notes.append("以下 marker 由情境的角色卡 / 世界書 / persona 填入，情境沒有對應內容時組裝為空：" + "、".join(unfilled))

    return Preset(name=name or "匯入的預設", blocks=blocks, params=params, options=options, extra=extra), notes


def _to_block(p: dict, enabled: bool) -> Block:
    ident = p["identifier"]
    content = p.get("content") or ""
    name = p.get("name") or ident
    is_marker = bool(p.get("marker")) and ident in MARKERS

    if is_marker:
        kind, marker = "marker", ident
    elif not content and _DIVIDER_NAME.match(name):
        kind, marker = "divider", None
    else:
        kind, marker = "content", None

    st = {k: copy.deepcopy(v) for k, v in p.items() if k not in BLOCK_KEYS}
    # 記住原始的 marker / enabled 欄位，匯出時原樣寫回
    for k in ("marker", "enabled"):
        if k in p:
            st[k] = p[k]

    role = p.get("role") or "system"
    return Block(
        id=ident,
        name=name,
        kind=kind,
        marker=marker,
        role=role if role in ("system", "user", "assistant") else "system",
        content=content,
        enabled=enabled,
        injection_position=int(p.get("injection_position") or 0),
        injection_depth=int(p.get("injection_depth", 4) if p.get("injection_depth") is not None else 4),
        injection_order=int(p.get("injection_order", 100) if p.get("injection_order") is not None else 100),
        # 實際預設的區塊常有上千字，匯入時先收合，避免 40 多張卡片全部展開
        collapsed=kind == "content" and bool(content),
        st=st,
    )


def export_preset(preset: Preset) -> dict:
    data = copy.deepcopy(preset.extra)
    for st, ours in PARAM_KEYS.items():
        data[st] = getattr(preset.params, ours)
    for st, ours in OPTION_KEYS.items():
        data[st] = getattr(preset.options, ours)

    prompts = []
    for b in preset.blocks:
        p = {
            "identifier": b.id,
            "name": b.name,
            "system_prompt": b.st.get("system_prompt", False),
        }
        if b.kind == "marker":
            p["marker"] = True
        else:
            p.update(
                role=b.role,
                content=b.content,
                injection_position=b.injection_position,
                injection_depth=b.injection_depth,
                injection_order=b.injection_order,
            )
            if "marker" in b.st:
                p["marker"] = b.st["marker"]
        for k, v in b.st.items():
            p.setdefault(k, copy.deepcopy(v))
        prompts.append(p)
    data["prompts"] = prompts

    order_entry = {
        "character_id": GLOBAL_ORDER_ID,
        "order": [{"identifier": b.id, "enabled": b.enabled} for b in preset.blocks],
    }
    data["prompt_order"] = [*data.get("prompt_order", []), order_entry]
    return data
