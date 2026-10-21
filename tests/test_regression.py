"""重試原因與統計、迴歸試算（probe）、部分迴歸的驗證輪次。"""

import itertools
import json
import time

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings

from fake_llm import FakeLLM, openrouter_stream

counter = itertools.count(1)


def responder(body):
    n = next(counter)
    last = body["messages"][-1]["content"]
    sysmsg = body["messages"][0]["content"]
    if "請寫出" in last:
        return 200, openrouter_stream(f"模擬發言{n}", "", reasoning="")
    tag = "【新版】" if "（新版規則）" in sysmsg else ""
    return 200, openrouter_stream(f"{tag}角色回覆{n}", "", reasoning="")


@pytest.fixture
def c(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    app = create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"), http_transport=FakeLLM(default=responder).transport)
    with TestClient(app) as client:
        yield client


def wait(c, eid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/experiments/{eid}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("沒有結束")


def setup_two_stage(c):
    ws = c.get("/api/workspace").json()
    c.post("/api/vcs/commit", json={"message": "C0"})
    sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["嗨"]}).json()["id"]
    cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
    c.put("/api/user-sim", json={"connection_id": cid})
    script = c.post("/api/scripts", json={"name": "r", "data": {"stages": [
        {"turns": 1, "seeds": 2, "promote": {"mode": "manual"}}, {"turns": 3, "seeds": 1}]}}).json()
    eid = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()["id"]
    d = wait(c, eid)
    c.post(f"/api/experiments/{eid}/advance", json={"run_ids": [r["id"] for r in d["runs"]]})
    return wait(c, eid), ws


def test_reasons_and_stats(c):
    assert "文風不佳" in c.get("/api/retry-reasons").json()["reasons"]
    c.put("/api/retry-reasons", json={"reasons": ["太花俏", " ", "格式壞了"]})
    assert c.get("/api/retry-reasons").json()["reasons"] == ["太花俏", "格式壞了"]

    d, _ = setup_two_stage(c)
    s1 = [r for r in d["runs"] if r["stage"] == 1]
    # 階段層級：整條分支重試，帶原因
    r = c.post(f"/api/experiments/{d['id']}/retry", json={"stage": {"stage": 1, "kind": "branch"}, "tags": ["太花俏"], "note": "譬喻太多"}).json()
    assert sorted(r["started"]) == sorted(x["id"] for x in s1)
    wait(c, d["id"])
    for x in s1:
        m = c.get(f"/api/runs/{x['id']}").json()["messages"]
        assert [(y["turn"], y["role"]) for y in m] == [(2, "user"), (2, "assistant"), (3, "user"), (3, "assistant")]
    # 單則重試一次，不帶原因
    c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": s1[0]["id"], "turn": 3, "role": "assistant"}]})
    wait(c, d["id"])

    st = c.get(f"/api/experiments/{d['id']}/retry-stats").json()
    m = next(x for x in st["models"] if x["model"] == "m")
    # 角色回覆：階段 1 兩條 + 階段 2 兩條 × 2 回合 = 6，重試又產生 2×2 + 1 = 5，封存 5 → 共 11 則
    assert m["manual"] == 3 and m["tags"] == {"太花俏": 2} and m["replies"] == 11
    assert m["manual_rate"] == pytest.approx(3 / 11, abs=0.001)
    assert st["providers"][0]["provider"] == "Z.AI" and st["providers"][0]["errors"] == 0
    assert c.get("/api/retry-stats").json()["models"]


def test_probe_and_verify_round(c):
    d, ws = setup_two_stage(c)
    eid = d["id"]
    s1 = sorted([r for r in d["runs"] if r["stage"] == 1], key=lambda r: r["id"])
    target = s1[0]
    orig = c.get(f"/api/runs/{target['id']}").json()["messages"]

    # 工作區還沒改 → diff 相同
    assert c.get(f"/api/experiments/{eid}/diff-workspace").json()["same"] is True
    preset = ws["preset"]
    b = next(x for x in preset["blocks"] if x["kind"] == "content" and x["role"] == "system")
    b["content"] += "（新版規則）"
    c.put("/api/workspace", json={"preset": preset, "context": ws["context"]})
    dw = c.get(f"/api/experiments/{eid}/diff-workspace").json()
    assert dw["same"] is False and dw["diff"]["summary"]["modified"] == 1

    # 試算第 3 回合：歷史與使用者發言相同，只換預設；不寫入實驗
    events = [json.loads(x[5:]) for x in c.post(f"/api/experiments/{eid}/probe", json={"run_id": target["id"], "turn": 3}).text.splitlines() if x.startswith("data:")]
    done = events[-1]["probe"]
    assert done["content"].startswith("【新版】") and done["turn"] == 3
    assert c.get(f"/api/runs/{target['id']}").json()["messages"] == orig
    c.post(f"/api/experiments/{eid}/probe", json={"run_id": target["id"], "turn": 3})
    assert len(c.get(f"/api/experiments/{eid}/probes?run_id={target['id']}&turn=3").json()) == 2
    assert c.post(f"/api/experiments/{eid}/probe", json={"run_id": target["id"], "turn": 9}).status_code == 422

    # 驗證輪次：只重跑階段 2 的一條分支
    v = c.post(f"/api/experiments/{eid}/verify", json={"from_stage": 1, "run_ids": [target["id"]]}).json()
    v = wait(c, v["id"])
    assert v["rerun_of"] == eid and v["current_stage"] == 1 and v["state"]["partial_from"] == 1
    assert v["status"] == "done" and len(v["runs"]) == 1
    nr = v["runs"][0]
    assert nr["counterpart"] == target["id"] and nr["origin_run_id"] == target["id"]
    new = c.get(f"/api/runs/{nr['id']}").json()
    # 沿用原對話（含第 1 階段）與使用者發言，只有角色回覆換成新版
    assert new["scenario"]["history"] == c.get(f"/api/runs/{target['id']}").json()["scenario"]["history"]
    assert [m["content"] for m in new["messages"] if m["role"] == "user"] == [m["content"] for m in orig if m["role"] == "user"]
    assert all(m["content"].startswith("【新版】") for m in new["messages"] if m["role"] == "assistant")
    assert c.post(f"/api/experiments/{eid}/verify", json={"from_stage": 5}).status_code == 422
