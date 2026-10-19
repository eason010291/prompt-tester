import copy

import pytest

from prompt_tester.build import build
from prompt_tester.preset import HistoryMessage
from prompt_tester.st_preset import PresetFormatError, export_preset, import_preset

# 縮小版的 ST 預設，結構仿照實際匯出檔（內建 prompt 欄位不齊、custom prompt 為 UUID、兩份 prompt_order）
ST = {
    "temperature": 0.8,
    "top_p": 0.95,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "openai_max_context": 60000,
    "openai_max_tokens": 10000,
    "squash_system_messages": True,
    "new_chat_prompt": "[Start a new Chat]",
    "names_behavior": 0,
    "wi_format": "{0}",
    "prompts": [
        {"name": "Main Prompt", "system_prompt": True, "role": "system", "identifier": "main", "content": "",
         "injection_position": 0, "injection_depth": 4, "injection_order": 100, "injection_trigger": [], "forbid_overrides": False},
        {"name": "Auxiliary Prompt", "system_prompt": True, "role": "system", "identifier": "nsfw", "content": ""},
        {"identifier": "chatHistory", "name": "Chat History", "system_prompt": True, "marker": True},
        {"identifier": "charDescription", "name": "Char Description", "system_prompt": True, "marker": True},
        {"name": "Post-History Instructions", "system_prompt": True, "role": "system", "identifier": "jailbreak", "content": ""},
        {"identifier": "u-style", "name": "文風--succubus改", "role": "system", "content": "寫給 {{char}} 的文風",
         "system_prompt": False, "marker": False, "injection_position": 0, "injection_depth": 4,
         "injection_order": 100, "injection_trigger": [], "forbid_overrides": False, "enabled": False},
        {"identifier": "u-div", "name": "===文風指示===", "role": "system", "content": "", "system_prompt": False,
         "marker": False, "injection_position": 0, "injection_depth": 4, "injection_order": 100,
         "injection_trigger": [], "forbid_overrides": False, "enabled": False},
        {"identifier": "u-body", "name": "【正文】(常駐)", "role": "system", "content": "<p class=\"narrative\">",
         "system_prompt": False, "marker": False, "injection_position": 0, "injection_depth": 4,
         "injection_order": 100, "injection_trigger": [], "forbid_overrides": False},
        {"identifier": "u-orphan", "name": "孤兒", "role": "system", "content": "不在 order 中", "system_prompt": False},
        {"identifier": "u-sys", "name": "自訂 system_prompt", "role": "system", "content": "ST 不會送出",
         "system_prompt": True, "marker": False},
        {"identifier": "u-cont", "name": "只在 continue", "role": "system", "content": "continue only",
         "system_prompt": False, "injection_trigger": ["continue"]},
    ],
    "prompt_order": [
        {"character_id": 100000, "order": [{"identifier": "main", "enabled": True}, {"identifier": "chatHistory", "enabled": True}]},
        {"character_id": 100001, "order": [
            {"identifier": "u-div", "enabled": False},
            {"identifier": "u-style", "enabled": True},
            {"identifier": "main", "enabled": True},
            {"identifier": "nsfw", "enabled": True},
            {"identifier": "charDescription", "enabled": True},
            {"identifier": "chatHistory", "enabled": True},
            {"identifier": "jailbreak", "enabled": True},
            {"identifier": "u-body", "enabled": True},
            {"identifier": "u-sys", "enabled": True},
            {"identifier": "u-cont", "enabled": True},
            {"identifier": "missing", "enabled": True},
        ]},
    ],
    "extensions": {"regex_scripts": [{"id": "r1", "scriptName": "CSS", "findRegex": "(</div>?)", "placement": [2]}]},
}


def test_import_uses_order_100001_and_its_enabled_flags():
    p, notes = import_preset(copy.deepcopy(ST), "聿V1")
    assert p.name == "聿V1"
    assert [b.id for b in p.blocks] == [
        "u-div", "u-style", "main", "nsfw", "charDescription", "chatHistory", "jailbreak", "u-body", "u-sys", "u-cont",
        "u-orphan",
    ]
    by_id = {b.id: b for b in p.blocks}
    # prompts[].enabled=False 不影響，order 的 enabled 才算
    assert by_id["u-style"].enabled is True
    assert by_id["u-orphan"].enabled is False
    assert by_id["u-div"].kind == "divider"
    assert by_id["chatHistory"].kind == "marker" and by_id["chatHistory"].marker == "chatHistory"
    assert by_id["charDescription"].marker == "charDescription"
    assert p.params.temperature == 0.8 and p.params.max_tokens == 10000 and p.params.max_context == 60000
    assert p.options.squash_system_messages is True
    joined = "\n".join(notes)
    assert "missing" in joined and "不在 prompt_order" in joined and "正則" in joined and "charDescription" in joined


def test_build_of_imported_preset_follows_st_rules():
    p, _ = import_preset(copy.deepcopy(ST))
    r = build(p, [HistoryMessage(role="user", content="嗨")], {"char": "聿", "user": "藍"})
    contents = [m.content for m in r.messages]
    # 空的 main / nsfw / jailbreak 被丟掉；squash 合併連續 system；
    # system_prompt:true 的自訂區塊與只在 continue 觸發的區塊不送
    assert contents == ["寫給 聿 的文風", "[Start a new Chat]", "嗨", '<p class="narrative">']
    assert "ST 不會送出" not in "".join(contents) and "continue only" not in "".join(contents)


def test_export_round_trip_preserves_everything():
    original = copy.deepcopy(ST)
    p, _ = import_preset(copy.deepcopy(ST))
    out = export_preset(p)

    for key in original:
        if key not in ("prompts", "prompt_order"):
            assert out[key] == original[key], key

    orig_prompts = {x["identifier"]: x for x in original["prompts"]}
    out_prompts = {x["identifier"]: x for x in out["prompts"]}
    assert set(out_prompts) == set(orig_prompts)
    for ident, op in orig_prompts.items():
        for k, v in op.items():
            assert out_prompts[ident][k] == v, (ident, k)

    orders = {o["character_id"]: o["order"] for o in out["prompt_order"]}
    assert orders[100000] == original["prompt_order"][0]["order"]
    assert [e["identifier"] for e in orders[100001]][:10] == [
        e["identifier"] for e in original["prompt_order"][1]["order"] if e["identifier"] != "missing"
    ]

    again, _ = import_preset(out)
    assert again.model_dump(exclude={"name"}) == p.model_dump(exclude={"name"})


def test_rejects_non_preset():
    with pytest.raises(PresetFormatError):
        import_preset({"spec": "chara_card_v3"})
