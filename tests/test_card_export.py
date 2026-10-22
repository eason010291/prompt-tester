"""角色卡匯出（ST 1.18.0 格式）：V3 JSON、PNG（chara + ccv3）、來回匯入一致、未知欄位保留。"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from prompt_tester import st_card
from prompt_tester.app import create_app
from prompt_tester.config import Settings

RAW = {
    "spec": "chara_card_v3",
    "spec_version": "3.0",
    "create_date": "2026-1-1 @12h 00m 00s 000ms",
    "data": {
        "name": "露",
        "description": "露是二十五歲的插畫家。",
        "personality": "溫柔",
        "scenario": "頂樓工作室",
        "first_mes": "（露抬頭）你來了。",
        "mes_example": "<START>\n{{char}}: 嗯。",
        "creator_notes": "作者備註",
        "system_prompt": "系統提示",
        "post_history_instructions": "後置指示",
        "alternate_greetings": ["（露在畫畫）", "（露睡著了）"],
        "group_only_greetings": [],
        "tags": ["原創", "成人"],
        "creator": "聿",
        "character_version": "1.2",
        "extensions": {
            "talkativeness": "0.5",
            "fav": True,
            "world": "露的世界",
            "depth_prompt": {"prompt": "記得她怕雷", "depth": 2, "role": "system"},
            "regex_scripts": [{"scriptName": "狀態欄", "findRegex": "/<s>(.*)<\\/s>/g", "replaceString": "$1"}],
            "tavern_helper": {"scripts": [{"name": "x"}]},
        },
        "character_book": {
            "name": "露的世界",
            "extensions": {"custom": 1},
            "entries": [
                {"id": 0, "keys": ["工作室"], "secondary_keys": [], "comment": "工作室", "content": "工作室在頂樓。",
                 "constant": False, "selective": True, "insertion_order": 100, "enabled": True, "position": "before_char",
                 "use_regex": True,
                 "extensions": {"position": 0, "depth": 4, "probability": 100, "useProbability": True, "selectiveLogic": 0,
                                "sticky": 3, "cooldown": None, "vectorized": False, "automation_id": "a1",
                                "match_character_description": True, "group": "地點", "group_weight": 50}},
                {"id": 7, "keys": ["貓"], "comment": "貓", "content": "養了一隻叫麻糬的貓。", "constant": True,
                 "selective": False, "insertion_order": 50, "enabled": False, "position": "after_char",
                 "extensions": {"position": 4, "depth": 1, "role": 1, "delay_until_recursion": True}},
            ],
        },
    },
}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(tmp_path / "none.toml"))
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"))) as client:
        yield client


def import_card(c, data: bytes, filename: str):
    r = c.post("/api/scenarios/import-card", json={"filename": filename, "data_base64": base64.b64encode(data).decode()})
    assert r.status_code == 200, r.text
    return r.json()["scenario"]


def test_json_v3_roundtrip(c):
    sc = import_card(c, json.dumps(RAW, ensure_ascii=False).encode(), "lu.json")
    r = c.get(f"/api/scenarios/{sc['id']}/export?format=json")
    assert r.headers["content-type"].startswith("application/json")
    assert "filename*=UTF-8''%E9%9C%B2.json" in r.headers["content-disposition"]
    out = r.json()
    # ST 結構：V3 規格、頂層 V1 欄位、data 完整
    assert out["spec"] == "chara_card_v3" and out["spec_version"] == "3.0"
    assert out["name"] == "露" and out["first_mes"] == RAW["data"]["first_mes"] and out["creatorcomment"] == "作者備註"
    assert out["fav"] is False and out["data"]["extensions"]["fav"] is False and "chat" not in out
    d = out["data"]
    assert d["system_prompt"] == "系統提示" and d["alternate_greetings"] == RAW["data"]["alternate_greetings"]
    assert d["extensions"]["depth_prompt"] == {"prompt": "記得她怕雷", "depth": 2, "role": "system"}
    assert d["extensions"]["regex_scripts"] == RAW["data"]["extensions"]["regex_scripts"]
    # 本工具不認得的欄位原樣保留
    assert d["extensions"]["tavern_helper"] == {"scripts": [{"name": "x"}]}
    assert out["create_date"] == RAW["create_date"] and d["creator"] == "聿" and d["tags"] == ["原創", "成人"]
    book = d["character_book"]
    assert book["extensions"] == {"custom": 1}
    e0, e7 = book["entries"]
    assert e0["extensions"]["sticky"] == 3 and e0["extensions"]["automation_id"] == "a1"
    assert e0["extensions"]["match_character_description"] is True and e0["extensions"]["group_weight"] == 50
    assert e7["id"] == 7 and e7["enabled"] is False and e7["position"] == "after_char"
    assert e7["extensions"]["position"] == 4 and e7["extensions"]["delay_until_recursion"] is True

    # 來回：匯出的卡再匯入，內容相同
    again = import_card(c, json.dumps(out, ensure_ascii=False).encode(), "lu2.json")
    a, b = sc["card"], again["card"]
    for key in ("name", "description", "personality", "scenario", "first_mes", "mes_example", "creator_notes",
                "system_prompt", "post_history_instructions", "alternate_greetings", "depth_prompt", "regex_scripts", "book"):
        assert a[key] == b[key], key
    assert again["opening"] == sc["opening"]

    # V2 匯出
    v2 = c.get(f"/api/scenarios/{sc['id']}/export?format=json&spec=v2").json()
    assert v2["spec"] == "chara_card_v2" and v2["spec_version"] == "2.0"


def test_export_reflects_edits_and_opening(c):
    sc = import_card(c, json.dumps(RAW, ensure_ascii=False).encode(), "lu.json")
    sc["card"]["description"] = "露是作家。"
    sc["card"]["book"]["entries"][0]["content"] = "工作室有天窗。"
    # 切換到替代開場白 → first_mes 維持原卡
    sc["opening"] = RAW["data"]["alternate_greetings"][0]
    c.put(f"/api/scenarios/{sc['id']}", json=sc)
    out = c.get(f"/api/scenarios/{sc['id']}/export").json()
    assert out["data"]["description"] == "露是作家。" and out["description"] == "露是作家。"
    assert out["data"]["character_book"]["entries"][0]["content"] == "工作室有天窗。"
    assert out["data"]["first_mes"] == RAW["data"]["first_mes"]
    # 改寫過的開場白 → 寫回 first_mes
    sc["opening"] = "（露揉眼睛）你終於來了。"
    c.put(f"/api/scenarios/{sc['id']}", json=sc)
    assert c.get(f"/api/scenarios/{sc['id']}/export").json()["first_mes"] == "（露揉眼睛）你終於來了。"


def test_png_roundtrip(c):
    v2 = dict(RAW, spec="chara_card_v2", spec_version="2.0")
    image = st_card.placeholder_png(4, 4, (10, 20, 30))
    png = st_card.write_png_card(image, v2, RAW)
    sc = import_card(c, png, "露.png")
    assert c.get(f"/api/scenarios/{sc['id']}/card-image").json() == {"has_image": True, "filename": "露.png"}
    r = c.get(f"/api/scenarios/{sc['id']}/export?format=png")
    assert r.headers["content-type"] == "image/png" and r.headers["x-card-image"] == "original"
    chunks = st_card.read_png_text_chunks(r.content)
    chara = json.loads(base64.b64decode(chunks["chara"]))
    ccv3 = json.loads(base64.b64decode(chunks["ccv3"]))
    assert chara["spec"] == "chara_card_v2" and ccv3["spec"] == "chara_card_v3" and ccv3["spec_version"] == "3.0"
    assert ccv3["data"]["description"] == RAW["data"]["description"]
    # 圖片本身沒變（只換卡片資料）：IDAT 相同
    idat = lambda b: b[b.index(b"IDAT") - 4 : b.index(b"IEND")]  # noqa: E731
    assert idat(r.content).startswith(idat(image)[: len(idat(image)) - 0][:20])
    assert r.content.count(b"tEXtchara") == 1 and r.content.count(b"tEXtccv3") == 1
    again = import_card(c, r.content, "again.png")
    assert again["card"]["book"] == sc["card"]["book"]

    # 沒有原圖（從 JSON 匯入）→ 預設圖
    js = import_card(c, json.dumps(RAW, ensure_ascii=False).encode(), "lu.json")
    r = c.get(f"/api/scenarios/{js['id']}/export?format=png")
    assert r.headers["x-card-image"] == "placeholder" and "ccv3" in st_card.read_png_text_chunks(r.content)

    manual = c.post("/api/scenarios", json={"name": "無卡"}).json()
    assert c.get(f"/api/scenarios/{manual['id']}/export").status_code == 422
