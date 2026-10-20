import base64
import json
import struct
import zlib

import pytest

from prompt_tester.build import build, parse_example_block, parse_mes_examples
from prompt_tester.entities import CharacterCard, DepthPrompt, LoreEntry, Lorebook
from prompt_tester.preset import Block, HistoryMessage, Options, Preset
from prompt_tester.st_card import CardFormatError, load_card_bytes, parse_card

V3 = {
    "spec": "chara_card_v3",
    "spec_version": "3.0",
    "data": {
        "name": "露絲菲娜",
        "description": "{{char}}，27 歲。",
        "personality": "直率",
        "scenario": "酒館",
        "first_mes": "嗨。",
        "mes_example": "<START>\n{{user}}: 你好\n{{char}}: 嗨\n<START>\n{{char}}: 第二段",
        "system_prompt": "卡片主指令。{{original}}",
        "post_history_instructions": "",
        "alternate_greetings": ["另一個開場"],
        "extensions": {"depth_prompt": {"prompt": "保持角色。", "depth": 1, "role": "system"}},
        "character_book": {"name": "書", "entries": [
            {"id": 0, "keys": [], "content": "世界觀", "constant": True, "enabled": True, "position": "before_char",
             "insertion_order": 0, "extensions": {}},
            {"id": 1, "keys": ["酒"], "content": "酒的設定", "constant": False, "enabled": True,
             "position": "after_char", "insertion_order": 10, "extensions": {}},
        ]},
    },
}


def png_with(chunks: dict[str, str]) -> bytes:
    def chunk(ctype: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body))

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0" * 13)
    for key, value in chunks.items():
        out += chunk(b"tEXt", key.encode() + b"\0" + value.encode())
    return out + chunk(b"IEND", b"")


def b64(d: dict) -> str:
    return base64.b64encode(json.dumps(d, ensure_ascii=False).encode()).decode()


def test_png_prefers_ccv3():
    v2 = {**V3, "spec": "chara_card_v2", "data": {**V3["data"], "name": "舊"}}
    raw = load_card_bytes(png_with({"chara": b64(v2), "ccv3": b64(V3)}))
    assert raw["data"]["name"] == "露絲菲娜"


def test_png_without_card_rejected():
    with pytest.raises(CardFormatError):
        load_card_bytes(png_with({"Software": "x"}))


def test_json_with_bom_and_v1():
    raw = load_card_bytes("﻿".encode() + json.dumps({"name": "舊卡", "first_mes": "hi"}).encode())
    card, notes = parse_card(raw)
    assert card.name == "舊卡" and card.spec == "v1" and notes


def test_parse_card_fields():
    card, _ = parse_card(V3)
    assert card.depth_prompt == DepthPrompt(prompt="保持角色。", depth=1, role="system")
    assert len(card.book.entries) == 2 and card.alternate_greetings == ["另一個開場"]


def test_parse_examples_like_st():
    blocks = parse_mes_examples("藍: 你好\n露: 嗨\n還有一行")
    assert blocks == ["<START>\n藍: 你好\n露: 嗨\n還有一行\n"]
    msgs = parse_example_block(blocks[0], "藍", "露")
    assert [(m.name, m.content) for m in msgs] == [("example_user", "你好"), ("example_assistant", "嗨\n還有一行")]
    assert all(m.role == "system" for m in msgs)


def card_preset() -> Preset:
    def m(marker):
        return Block(id=marker, name=marker, kind="marker", marker=marker)

    return Preset(
        blocks=[
            Block(id="main", name="Main", content="預設主指令", st={"system_prompt": True}),
            m("worldInfoBefore"), m("charDescription"), m("charPersonality"), m("scenario"),
            m("personaDescription"), m("worldInfoAfter"), m("dialogueExamples"), m("chatHistory"),
        ],
        options=Options(squash_system_messages=False, wi_format="[WI]{0}"),
    )


def test_build_with_card_fills_markers_and_overrides():
    card, _ = parse_card(V3)
    r = build(
        card_preset(),
        [HistoryMessage(role="assistant", content="嗨。"), HistoryMessage(role="user", content="來杯酒")],
        {"char": "露絲菲娜", "user": "藍"},
        card=card,
        persona="藍是旅人",
    )
    got = [(m.role, m.name, m.content) for m in r.messages]
    assert got[0] == ("system", None, "卡片主指令。預設主指令")  # {{original}}
    assert ("system", None, "[WI]世界觀") in got
    assert ("system", None, "露絲菲娜，27 歲。") in got
    assert ("system", None, "直率") in got and ("system", None, "酒館") in got
    assert ("system", None, "藍是旅人") in got
    assert ("system", None, "[WI]酒的設定") in got  # 關鍵字「酒」由最新訊息觸發
    examples = [x for x in got if x[1]]
    assert examples == [("system", "example_user", "你好"), ("system", "example_assistant", "嗨"),
                        ("system", "example_assistant", "第二段")]
    assert got.count(("system", None, "[Example Chat]")) == 2
    # 深度提示 depth 1：插在最後一則之前
    tail = got[-3:]
    assert tail == [("assistant", None, "嗨。"), ("system", None, "保持角色。"), ("user", None, "來杯酒")]
    assert [a.entry.uid for a in r.worldinfo.activated] == [0, 1]


def test_forbid_overrides_keeps_preset_main():
    card, _ = parse_card(V3)
    p = card_preset()
    p.blocks[0].st["forbid_overrides"] = True
    r = build(p, [], {"char": "露絲菲娜", "user": "藍"}, card=card)
    assert r.messages[0].content == "預設主指令"


def test_wi_depth_entries_join_card_depth_prompt():
    card = CharacterCard(
        name="露",
        depth_prompt=DepthPrompt(prompt="角色提示", depth=0, role="system"),
        book=Lorebook(entries=[LoreEntry(uid=1, content="世界書深度", constant=True, enabled=True, position=4, depth=0)]),
    )
    r = build(card_preset(), [HistoryMessage(role="user", content="嗨")], {"char": "露", "user": "藍"}, card=card)
    # 同深度、同角色：依 key 字母順序 DEPTH_PROMPT < customDepthWI_0_0，以 \n 合併
    assert r.messages[-1].content == "角色提示\n世界書深度"


def test_warns_when_examples_have_no_character_lines():
    card = CharacterCard(name="唐心桃｜小惡魔", mes_example="<START>\n{{user}}: 你好\n唐心桃：「全形冒號」")
    r = build(card_preset(), [], {"char": "唐心桃｜小惡魔", "user": "藍"}, card=card)
    assert [m.name for m in r.messages if m.name] == ["example_user"]
    assert any("沒有辨識到角色台詞" in w and "半形冒號" in w for w in r.warnings)


def test_warns_about_prompt_only_regex():
    card = CharacterCard(name="露", regex_scripts=[
        {"scriptName": "改送出", "findRegex": "a", "replaceString": "b", "promptOnly": True, "placement": [2]},
        {"scriptName": "顯示", "findRegex": "a", "replaceString": "b", "markdownOnly": True, "placement": [2]},
    ])
    r = build(card_preset(), [], {"char": "露", "user": "藍"}, card=card)
    assert any("promptOnly" in w and "改送出" in w and "顯示" not in w for w in r.warnings)
