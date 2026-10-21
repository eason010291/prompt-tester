import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings
from prompt_tester.experiments import parse_judge
from prompt_tester.scripts import apply_overrides, compute_overrides

from fake_llm import FakeLLM, openrouter_stream


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    return tmp_path


def app(env, fake=None):
    return TestClient(create_app(Settings(data_dir=env / "data", web_dist=env / "dist"),
                                 http_transport=fake.transport if fake else None))


# ---- 腳本 ----

def test_override_paths():
    parent = {"stages": [{"turns": 1, "seeds": 8, "temperatures": []}, {"turns": 4, "seeds": 1, "temperatures": []}], "concurrency": 3}
    child = json.loads(json.dumps(parent))
    child["stages"][0]["seeds"] = 4
    child["stages"][1]["temperatures"] = [0.3, 1.0]
    ov = compute_overrides(parent, child)
    assert ov == {"stages[0].seeds": 4, "stages[1].temperatures": [0.3, 1.0]}
    assert apply_overrides(parent, ov)[0] == child
    # 階段數不同 → 整個 stages 覆寫
    child["stages"].append({"turns": 10, "seeds": 1, "temperatures": []})
    assert set(compute_overrides(parent, child)) == {"stages"}
    # 父腳本階段變少，子腳本的細項覆寫對不到 → 略過並回報
    data, warnings = apply_overrides({"stages": [{"seeds": 1}]}, {"stages[1].seeds": 3})
    assert data == {"stages": [{"seeds": 1}]} and "stages[1].seeds" in warnings[0]


def test_script_inheritance_api(env):
    with app(env) as c:
        scripts = {s["name"]: s for s in c.get("/api/scripts").json()}
        std, quick = scripts["金字塔-標準"], scripts["金字塔-快速"]
        assert quick["parent_id"] == std["id"] and quick["overrides"] == {"stages[0].seeds": 4}
        assert [s["seeds"] for s in quick["resolved"]["stages"]] == [4, 1, 1]

        # 父腳本改第二階段回合數 → 子腳本跟著變；子腳本的覆寫保留
        data = std["resolved"]
        data["stages"][1]["turns"] = 5
        c.put(f"/api/scripts/{std['id']}", json={"name": std["name"], "data": data})
        q = c.get(f"/api/scripts/{quick['id']}").json()
        assert q["resolved"]["stages"][1]["turns"] == 5 and q["resolved"]["stages"][0]["seeds"] == 4

        # 子腳本再覆寫溫度掃描，之後還原繼承
        qd = q["resolved"]
        qd["stages"][0]["temperatures"] = [0.5, 1.2]
        q = c.put(f"/api/scripts/{quick['id']}", json={"name": "金字塔-快速", "parent_id": std["id"], "data": qd}).json()
        assert q["overrides"] == {"stages[0].seeds": 4, "stages[0].temperatures": [0.5, 1.2]}
        q = c.put(f"/api/scripts/{quick['id']}", json={"name": "金字塔-快速", "parent_id": std["id"], "reset": ["stages[0].seeds"]}).json()
        assert q["overrides"] == {"stages[0].temperatures": [0.5, 1.2]} and q["resolved"]["stages"][0]["seeds"] == 8

        # 不能形成循環、有子腳本不能刪
        r = c.put(f"/api/scripts/{std['id']}", json={"name": std["name"], "parent_id": quick["id"]})
        assert r.status_code == 422
        assert c.delete(f"/api/scripts/{std['id']}").status_code == 422
        bad = c.post("/api/scripts", json={"name": "x", "data": {"stages": []}})
        assert bad.status_code == 422


def test_parse_judge():
    out = '<think>想一下</think>```json\n{"ranking": [{"candidate": 2, "pick": true, "reason": "格式完整"}, {"candidate": 1, "pick": false, "reason": "出戲"}, {"candidate": 9}]}\n```'
    assert parse_judge(out, [11, 12]) == {
        12: {"pick": True, "rank": 1, "reason": "格式完整"},
        11: {"pick": False, "rank": 2, "reason": "出戲"},
    }
    with pytest.raises(ValueError):
        parse_judge("我覺得第二個比較好", [1, 2])


# ---- 實驗 ----

def responder(body):
    msgs = body["messages"]
    last = msgs[-1]["content"]
    if "## 候選 1" in last:
        n = len(re.findall(r"^## 候選 \d+", last, re.M))
        ranking = [{"candidate": i, "pick": i == n, "reason": f"理由{i}"} for i in range(n, 0, -1)]
        text = json.dumps({"ranking": ranking}, ensure_ascii=False)
    elif "請寫出" in last:
        text = f"模擬使用者發言 t{len(msgs)}"
    else:
        text = f"角色回覆 seed={body.get('seed')} temp={body['temperature']}"
    return 200, openrouter_stream(text, "", reasoning="")


def wait(c, exp_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/experiments/{exp_id}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("實驗沒有結束")


def test_pyramid_experiment_flow(env):
    fake = FakeLLM(default=responder)
    with app(env, fake) as c:
        ws = c.get("/api/workspace").json()
        sid = c.post("/api/scenarios", json={"name": "s", "char": "露", "user": "藍", "user_turns": ["嗨"]}).json()["id"]
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        script = c.post("/api/scripts", json={"name": "測試", "data": {
            "concurrency": 2,
            "stages": [
                {"turns": 1, "seeds": 3, "promote": {"mode": "top_k", "k": 2}},
                {"turns": 3, "seeds": 1, "temperatures": [0.5, 1.0], "promote": {"mode": "llm_assist", "k": 1}},
                {"turns": 4, "seeds": 1},
            ],
        }}).json()

        # 第 2 回合起需要模擬使用者 → 沒設定時拒絕
        body = {"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}
        r = c.post("/api/experiments", json=body)
        assert r.status_code == 422 and "模擬使用者" in r.json()["detail"]
        c.put("/api/user-sim", json={"connection_id": cid})

        exp = c.post("/api/experiments", json=body).json()
        d = wait(c, exp["id"])
        assert d["status"] == "waiting" and d["current_stage"] == 0
        s0 = [r for r in d["runs"] if r["stage"] == 0]
        assert len(s0) == 3 and all(r["status"] == "done" for r in s0)
        assert sorted(r["seed"] for r in s0) == [1, 2, 3]
        # 試跑列表（情境頁）不含實驗的分支
        assert c.get(f"/api/runs?scenario_id={sid}&standalone=true").json() == []

        # 晉級驗證
        assert c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": []}).status_code == 422
        assert c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": [99999]}).status_code == 422

        picked = [s0[0]["id"], s0[2]["id"]]
        c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": picked})
        d = wait(c, exp["id"])
        s1 = [r for r in d["runs"] if r["stage"] == 1]
        assert len(s1) == 4  # 2 條 × 2 個溫度
        assert {r["parent_run_id"] for r in s1} == set(picked) and {r["temperature"] for r in s1} == {0.5, 1.0}
        assert {r["id"] for r in d["runs"] if r["promoted"]} == set(picked)

        # 接續分支：歷史含父分支的對話，回合數從 2 起算，使用者發言由 LLM 扮演
        child = c.get(f"/api/runs/{s1[0]['id']}?requests=true").json()
        assert [m["turn"] for m in child["messages"]] == [2, 2, 3, 3]
        assert [m["source"] for m in child["messages"] if m["role"] == "user"] == ["llm_user", "llm_user"]
        parent = c.get(f"/api/runs/{child['parent_run_id']}").json()
        assert [h["content"] for h in child["scenario"]["history"]][-2:] == [m["content"] for m in parent["messages"]]
        assert child["messages"][1]["request"]["temperature"] == child["temperature"]

        # llm_assist：階段結束後自動請評審預選（每個候選都有排序與理由，只挑 1 條）
        judge = d["state"]["judge"]["1"]
        assert judge["error"] is None and len(judge["picks"]) == 4
        chosen = [int(k) for k, v in judge["picks"].items() if v["pick"]]
        assert len(chosen) == 1

        c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": chosen})
        d = wait(c, exp["id"])
        assert d["status"] == "done"
        s2 = [r for r in d["runs"] if r["stage"] == 2]
        assert len(s2) == 1 and s2[0]["turns"] == 1 and s2[0]["last_turn"] == 4
        assert c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": [s2[0]["id"]]}).status_code == 422

        # 評語分數彙總到實驗
        c.post("/api/feedback", json={"target_type": "run", "target_id": s0[0]["id"], "text": "文風：8/10"})
        listed = {e["id"]: e for e in c.get("/api/experiments").json()}
        assert listed[exp["id"]]["score"] == 8 and listed[exp["id"]]["runs"] == 8

        # 重跑：沿用腳本快照與情境，分支依位置配對
        rerun = c.post("/api/experiments", json={"preset": ws["preset"], "rerun_of": exp["id"]}).json()
        d2 = wait(c, rerun["id"])
        assert d2["name"].endswith("（重跑）") and d2["script_hash"] == d["script_hash"]
        pairs = {r["seed"]: r["counterpart"] for r in d2["runs"]}
        assert pairs == {r["seed"]: r["id"] for r in s0}
        assert sorted(d2["origin_promoted"]) == sorted(picked + chosen)

        # 刪除實驗連同分支
        assert c.delete(f"/api/experiments/{exp['id']}").status_code == 204
        assert c.get(f"/api/runs/{s0[0]['id']}").status_code == 404


def test_restart_mode_and_turn_validation(env):
    fake = FakeLLM(default=responder)
    with app(env, fake) as c:
        ws = c.get("/api/workspace").json()
        sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["一", "二", "三"]}).json()["id"]
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        script = c.post("/api/scripts", json={"name": "r", "data": {"user_source": "scenario_all", "stages": [
            {"turns": 2, "seeds": 2, "promote": {"mode": "restart"}},
            {"turns": 3, "seeds": 1, "promote": {"mode": "all"}},
            {"turns": 2, "seeds": 1},
        ]}}).json()
        # 情境逐回合輸入足夠 → 不需要模擬使用者
        exp = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()
        wait(c, exp["id"])
        d = c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": []}).json()
        d = wait(c, exp["id"])
        s1 = [r for r in d["runs"] if r["stage"] == 1]
        assert len(s1) == 1 and s1[0]["parent_run_id"] is None and s1[0]["turns"] == 3
        msgs = c.get(f"/api/runs/{s1[0]['id']}").json()["messages"]
        assert [m["content"] for m in msgs if m["role"] == "user"] == ["一", "二", "三"]
        # 下一階段累計 2 回合 < 已完成的 3 回合 → 拒絕
        r = c.post(f"/api/experiments/{exp['id']}/advance", json={"run_ids": [s1[0]["id"]]})
        assert r.status_code == 422 and "累計回合數" in r.json()["detail"]


def test_cancel_marks_queued_and_running(env):
    import asyncio

    import httpx

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, content=openrouter_stream("回覆", "", reasoning=""), headers={"content-type": "text/event-stream"})

    with TestClient(create_app(Settings(data_dir=env / "data", web_dist=env / "dist"), http_transport=httpx.MockTransport(slow))) as c:
        ws = c.get("/api/workspace").json()
        sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["嗨"]}).json()["id"]
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        script = c.post("/api/scripts", json={"name": "c", "data": {"concurrency": 1, "stages": [{"turns": 1, "seeds": 4}]}}).json()
        exp = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()
        time.sleep(0.15)
        assert c.delete(f"/api/experiments/{exp['id']}").status_code == 422  # 執行中不能刪
        c.post(f"/api/experiments/{exp['id']}/cancel")
        d = wait(c, exp["id"])
        assert d["status"] == "cancelled"
        assert {r["status"] for r in d["runs"]} == {"cancelled"}
