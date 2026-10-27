"""情境（角色卡）的線性版本、角色卡修改工具、內建迴圈改角色卡、迴歸試算 / 驗證輪次套用新卡。"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings

from fake_llm import FakeLLM, openrouter_stream

CARD = {
    "name": "露",
    "description": "露是二十五歲的插畫家，說話常用比喻。",
    "personality": "溫柔、慢熱",
    "mes_example": "<START>\n{{char}}: 嗯。",
    "book": {"name": "露的世界", "entries": [
        {"uid": 1, "comment": "工作室", "keys": ["工作室"], "content": "工作室在頂樓，堆滿畫布。"},
        {"uid": 2, "comment": "貓", "keys": ["貓"], "content": "養了一隻叫麻糬的貓。"},
    ]},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    return tmp_path


def client(env, responder=None):
    fake = FakeLLM(default=responder) if responder else None
    return TestClient(create_app(Settings(data_dir=env / "data", web_dist=env / "dist"),
                                 http_transport=fake.transport if fake else None))


def new_scenario(c):
    return c.post("/api/scenarios", json={"name": "露", "char": "露", "user": "藍", "user_turns": ["嗨"],
                                          "opening": "（露抬頭）你來了。", "card": CARD}).json()


def test_revisions_api(env):
    with client(env) as c:
        sc = new_scenario(c)
        sid = sc["id"]
        st = c.get(f"/api/scenarios/{sid}/revisions").json()
        assert st["revisions"] == [] and st["dirty"] is True
        r1 = c.post(f"/api/scenarios/{sid}/revisions", json={"message": "初版"}).json()
        assert c.get(f"/api/scenarios/{sid}/revisions").json()["dirty"] is False
        assert c.post(f"/api/scenarios/{sid}/revisions", json={"message": "沒改"}).status_code == 422

        sc["card"]["description"] = "露是二十五歲的插畫家。"
        sc["card"]["book"]["entries"][1]["enabled"] = False
        sc["persona"] = "上班族"
        c.put(f"/api/scenarios/{sid}", json=sc)
        d = c.get(f"/api/scenarios/{sid}/diff?a={r1['id']}").json()
        assert {f["label"] for f in d["fields"]} == {"描述", "使用者人設"}
        assert d["card_changed"] and d["fixture_changed"]
        assert d["lore"] == [{**d["lore"][0], "uid": 2, "status": "modified", "changes": ["enabled"]}]
        r2 = c.post(f"/api/scenarios/{sid}/revisions", json={"message": "刪掉比喻"}).json()
        revs = c.get(f"/api/scenarios/{sid}/revisions").json()["revisions"]
        assert [(r["number"], r["message"]) for r in revs] == [(2, "刪掉比喻"), (1, "初版")]
        assert revs[0]["parent_id"] == r1["id"]

        restored = c.post(f"/api/scenarios/{sid}/revisions/{r1['id']}/restore").json()
        assert restored["card"]["description"] == CARD["description"] and restored["persona"] == ""
        assert c.get(f"/api/scenarios/{sid}/revisions").json()["dirty"] is True  # 還原後是工作內容，要再存才成新版
        assert c.get(f"/api/scenarios/{sid}/diff?a={r1['id']}&b={r2['id']}").json()["fields"]


def test_scenario_tool_all_fields(env):
    with client(env) as c:
        sid = new_scenario(c)["id"]

        def t(action, args=None, sc=sid, **h):
            return c.post("/api/agent/tools/scenario", json={"action": action, "scenario_id": sc, **(args or {})}, headers=h)

        card = t("read").json()["result"]
        assert card["card"]["description"].startswith("露是") and card["opening"] == "（露抬頭）你來了。"
        assert [x["uid"] for x in card["book"]["entries"]] == [1, 2] and card["unsaved_changes"] is True
        assert card["fixtures"]["user_turns"] == ["嗨"] and card["fixtures"]["editable"] is True
        assert "raw" not in card["card"]

        # 角色卡所有欄位、開場白、世界書（含新增 / 刪除）
        assert t("patch", {"path": "card.description", "find": "，說話常用比喻", "replace": ""}).status_code == 200
        assert t("patch", {"path": "opening", "find": "你來了", "replace": "來啦"}).status_code == 200
        assert t("patch", {"path": "lore[1].content", "find": "堆滿畫布", "replace": "有一扇天窗"}).status_code == 200
        assert t("set", {"path": "lore[2].enabled", "value": False}).json()["result"]["new"] is False
        assert t("set", {"path": "lore[2].position", "value": 4}).status_code == 200
        assert "0～7" in t("set", {"path": "lore[2].position", "value": 9}).json()["detail"]
        assert t("set", {"path": "card.depth_prompt.depth", "value": 2}).status_code == 200
        assert t("set", {"path": "card.tags", "value": ["原創"]}).status_code == 200
        assert t("set", {"path": "card.name", "value": "小露"}).status_code == 200
        assert t("add", {"path": "card.alternate_greetings", "value": "（露在睡覺）"}).json()["result"]["index"] == 0
        added = t("add", {"path": "lore", "value": {"comment": "雷", "keys": ["雷"], "content": "露怕打雷。"}}).json()["result"]
        assert added["uid"] == 3
        assert t("remove", {"path": "lore[1]"}).json()["result"]["removed"] == "工作室"
        assert t("read", {"path": "lore[9]"}).status_code == 422
        assert t("read", {"path": "lore[3].keys"}).json()["result"] == ["雷"]
        assert "不能修改" in t("set", {"path": "lore[2].uid", "value": 7}).json()["detail"]
        assert "不能修改" in t("set", {"path": "card.raw", "value": {}}).json()["detail"]
        assert "沒有欄位" in t("set", {"path": "card.descripton", "value": "x"}).json()["detail"]
        assert "不合法" in t("set", {"path": "card.depth_prompt.role", "value": "narrator"}).json()["detail"]
        assert "不是文字" in t("patch", {"path": "card.tags", "find": "原", "replace": "x"}).json()["detail"]

        done = t("commit", {"message": "去掉比喻設定"}, **{"X-Agent-Name": "cc"}).json()["result"]
        rev = c.get(f"/api/scenarios/{sid}/revisions/{done['revision']}").json()
        assert rev["author"] == "agent:cc" and rev["data"]["card"]["description"] == "露是二十五歲的插畫家。"
        assert rev["data"]["opening"] == "（露抬頭）來啦。" and rev["data"]["card"]["book"]["entries"][-1]["content"] == "露怕打雷。"
        assert "note" not in done
        assert t("set", {"path": "card.regex_scripts", "value": [{"scriptName": "狀態欄", "findRegex": "/x/g"}]}).status_code == 200
        assert t("set", {"path": "card.regex_scripts[0].disabled", "value": True}).status_code == 200  # 正則是自由物件，可加鍵
        d = t("diff").json()["result"]
        assert [f["label"] for f in d["fields"]] == ["角色正則"] and not d["fixture_changed"]
        assert t("commit", {"message": "加正則"}).status_code == 200
        assert t("commit", {"message": "x"}).status_code == 422  # 沒有變更
        exported = c.get(f"/api/scenarios/{sid}/export").json()
        assert exported["data"]["tags"] == ["原創"] and exported["name"] == "小露"
        assert exported["data"]["extensions"]["depth_prompt"]["depth"] == 2

        # 測試條件（預設開放）：提交時標示
        assert t("set", {"path": "user_turns[0]", "value": "晚安"}).status_code == 200
        assert t("set", {"path": "vars.城市", "value": "台北"}).status_code == 200
        assert t("remove", {"path": "vars.城市"}).status_code == 200
        assert t("set", {"path": "persona", "value": "夜班護理師"}).status_code == 200
        d = t("diff").json()["result"]
        assert d["fixture_changed"] and {f["path"] for f in d["fields"]} == {"user_turns", "persona"}
        done = t("commit", {"message": "改成夜班情境"}).json()["result"]
        assert "不能和之前直接比較" in done["note"]
        rev = c.get(f"/api/scenarios/{sid}/revisions/{done['revision']}").json()
        assert rev["message"].startswith("【測試條件變更】") and rev["meta"]["fixture_changed"] is True
        assert [r["number"] for r in t("revisions").json()["result"]] == [3, 2, 1]

        s = c.get("/api/agent/settings").json()
        c.put("/api/agent/settings", json=s | {"allow_fixtures": False})
        assert "測試條件不開放" in t("set", {"path": "history", "value": []}).json()["detail"]
        assert t("read").json()["result"]["fixtures"]["editable"] is False

        # 手動建立的情境：沒有角色卡，但測試條件可讀
        manual = c.post("/api/scenarios", json={"name": "無卡"}).json()["id"]
        assert t("read", sc=manual).json()["result"]["card"] is None
        assert "沒有角色卡" in t("set", {"path": "card.description", "value": "x"}, sc=manual).json()["detail"]
        assert {sid, manual} <= {x["id"] for x in t("list").json()["result"]}


def test_add_lore_creates_book(env):
    with client(env) as c:
        sc = c.post("/api/scenarios", json={"name": "空書", "card": {"name": "阿木", "description": "木匠"}}).json()
        r = c.post("/api/agent/tools/scenario", json={"action": "add", "scenario_id": sc["id"], "path": "lore",
                                                       "value": {"comment": "工坊", "keys": ["工坊"], "content": "在河邊。"}})
        assert r.json()["result"]["uid"] == 0
        book = c.get(f"/api/scenarios/{sc['id']}").json()["card"]["book"]
        assert book["name"] == "阿木" and book["entries"][0]["content"] == "在河邊。"


def completion(content="", calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                             for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"cost": 0.001}}


def wait(c, path, key="status", timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(path).json()
        if d[key] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("沒有結束")


def test_builtin_loop_card_target_and_probe(env):
    state = {"sid": None, "plan": []}

    def responder(body):
        if body.get("stream") is False:
            names = {t["function"]["name"] for t in body["tools"]}
            # 改角色卡的一輪：只有角色卡與唯讀工具
            assert names == {"scenario", "prompt", "experiment"}
            enums = {t["function"]["name"]: t["function"]["parameters"]["properties"]["action"]["enum"] for t in body["tools"]}
            assert {"set", "patch", "add", "remove", "commit"} <= set(enums["scenario"]) and enums["prompt"] == ["read", "preview"]
            step = state["plan"].pop(0)
            return 200, json.dumps(step(body) if callable(step) else step, ensure_ascii=False).encode()
        text = "【新卡】回覆" if "沉默寡言" in json.dumps(body["messages"], ensure_ascii=False) else "回覆"
        return 200, openrouter_stream(text, "", reasoning="")

    with client(env, responder) as c:
        sid = new_scenario(c)["id"]
        state["sid"] = sid
        other = new_scenario(c)["id"]
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        c.put("/api/agent/settings", json=c.get("/api/agent/settings").json() | {"connection_id": cid})
        ws = c.get("/api/workspace").json()
        # 預設要有角色卡的 marker，卡片內容才會送出
        ws["preset"]["blocks"].insert(0, {"name": "Char Personality", "kind": "marker", "marker": "charPersonality"})
        ws = c.put("/api/workspace", json={"preset": ws["preset"], "context": ws["context"]}).json()
        script = c.post("/api/scripts", json={"name": "r", "data": {"stages": [{"turns": 1, "seeds": 1}]}}).json()
        exp = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()
        d = wait(c, f"/api/experiments/{exp['id']}")
        rev0 = d["state"]["scenario_revisions"][str(sid)]
        assert c.get(f"/api/scenarios/{sid}/revisions").json()["revisions"][0]["id"] == rev0  # 建立實驗時自動存成版本
        run = d["runs"][0]
        c.post("/api/feedback", json={"target_type": "run", "target_id": run["id"], "text": "文風：4/10\n比喻太多"})

        state["plan"] = [
            lambda body: (
                assert_card_context(body),
                completion("", [("scenario", {"action": "patch", "scenario_id": other, "path": "card.personality", "find": "溫柔", "replace": "x"}),
                                ("scenario", {"action": "patch", "path": "card.personality", "find": "溫柔、慢熱", "replace": "沉默寡言"})]),
            )[1],
            completion("", [("scenario", {"action": "commit", "message": "個性改為沉默寡言，減少比喻"})]),
        ]
        r = c.post("/api/agent/rounds", json={"experiment_id": exp["id"], "target": "card", "scenario_id": sid, "auto_rerun": True}).json()
        assert r["target"] == "card" and r["base_revision"] == rev0 and r["branch"] is None
        done = wait(c, f"/api/agent/rounds/{r['id']}")
        assert done["status"] == "done", done
        assert done["steps"][0]["calls"][0]["ok"] is False  # 別的情境不能改
        rev = c.get(f"/api/scenarios/{sid}/revisions/{done['result_revision']}").json()
        assert rev["author"] == "llm:m" and rev["data"]["card"]["personality"] == "沉默寡言"
        assert c.get(f"/api/scenarios/{other}").status_code in (200, 405)  # 另一個情境沒被動到
        assert done["result_commit"] is None

        # 自動重跑：新實驗用新卡
        rerun = wait(c, f"/api/experiments/{done['rerun_experiment_id']}")
        assert rerun["state"]["scenarios"][str(sid)]["card"]["personality"] == "沉默寡言"

        # 迴歸試算：預設沒變、角色卡變了 → 試算用新卡
        dw = c.get(f"/api/experiments/{exp['id']}/diff-workspace?scenario_id={sid}").json()
        assert dw["same"] is False and dw["same_preset"] is True and [f["label"] for f in dw["card"]["fields"]] == ["個性"]
        events = [json.loads(x[5:]) for x in c.post(f"/api/experiments/{exp['id']}/probe", json={"run_id": run["id"], "turn": 1}).text.splitlines() if x.startswith("data:")]
        assert events[-1]["probe"]["content"].startswith("【新卡】")
        v = c.post(f"/api/experiments/{exp['id']}/verify", json={"from_stage": 0, "run_ids": [run["id"]]}).json()
        v = wait(c, f"/api/experiments/{v['id']}")
        new_run = c.get(f"/api/runs/{v['runs'][0]['id']}").json()
        assert new_run["scenario"]["card"]["personality"] == "沉默寡言"
        assert new_run["messages"][-1]["content"].startswith("【新卡】")

        # 捨棄：沒提交的角色卡修改還原到基準版本
        state["plan"] = [completion("", [("scenario", {"action": "patch", "scenario_id": sid, "path": "card.description", "find": "插畫家", "replace": "作家"})]),
                         completion("改好了")]
        r2 = c.post("/api/agent/rounds", json={"instructions": "改職業", "target": "card", "scenario_id": sid}).json()
        d2 = wait(c, f"/api/agent/rounds/{r2['id']}")
        assert d2["status"] == "uncommitted"
        c.post(f"/api/agent/rounds/{r2['id']}/discard")
        cur = next(s for s in c.get("/api/scenarios").json() if s["id"] == sid)
        assert "插畫家" in cur["card"]["description"]


def assert_card_context(body):
    first = body["messages"][1]["content"]
    assert "這一輪的修改對象：情境" in first and "【目前的情境（scenario read）】" in first and "比喻太多" in first
    assert "測試條件（既有歷史" in first
