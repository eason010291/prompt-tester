import itertools
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
    if "請寫出" in last:
        text = f"模擬發言{n}"
    else:
        text = f"角色回覆{n} seed={body.get('seed')}"
    return 200, openrouter_stream(text, "", reasoning="")


@pytest.fixture
def c(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    app = create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"), http_transport=FakeLLM(default=responder).transport)
    with TestClient(app) as client:
        yield client


def wait(c, exp_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/experiments/{exp_id}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("沒有結束")


def start(c, stages):
    ws = c.get("/api/workspace").json()
    sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["嗨"]}).json()["id"]
    cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
    c.put("/api/user-sim", json={"connection_id": cid})
    script = c.post("/api/scripts", json={"name": "r", "data": {"stages": stages}}).json()
    exp = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()
    return wait(c, exp["id"])


def msgs(c, rid):
    return c.get(f"/api/runs/{rid}").json()["messages"]


def test_single_retry_keeps_later_user_turns(c):
    d = start(c, [{"turns": 3, "seeds": 2}])
    a, b = d["runs"][0]["id"], d["runs"][1]["id"]
    before_a, before_b = msgs(c, a), msgs(c, b)
    assert [(m["turn"], m["role"]) for m in before_a] == [(1, "user"), (1, "assistant"), (2, "user"), (2, "assistant"), (3, "user"), (3, "assistant")]
    keep_fb = c.post("/api/feedback", json={"target_type": "run_message", "target_id": before_a[1]["id"], "text": "文風：7/10"}).json()
    gone_fb = c.post("/api/feedback", json={"target_type": "run_message", "target_id": before_a[3]["id"], "text": "文風：3/10"}).json()

    r = c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": a, "turn": 2, "role": "assistant"}], "later_users": "keep"}).json()
    assert r["started"] == [a] and r["skipped"] == []
    d = wait(c, d["id"])
    assert d["status"] == "done"
    after = msgs(c, a)
    assert [m["content"] for m in after[:3]] == [m["content"] for m in before_a[:3]]  # 之前的保留
    assert after[3]["content"] != before_a[3]["content"] and after[5]["content"] != before_a[5]["content"]
    assert after[4]["content"] == before_a[4]["content"] and after[4]["source"] == before_a[4]["source"]  # 第 3 回合沿用
    assert [m["idx"] for m in after] == [1, 2, 3, 4, 5, 6]
    # 換 seed（避免回傳相同內容）
    req = c.get(f"/api/runs/{a}?requests=true").json()["messages"][3]["request"]
    assert req["seed"] == 1 + 1000 + 2
    assert msgs(c, b) == before_b  # 其他分支不動
    # 封存舊訊息；被取代訊息上的評語一起移除
    arch = c.get(f"/api/runs/{a}/archive").json()
    assert [(x["turn"], x["role"]) for x in arch] == [(2, "assistant"), (3, "user"), (3, "assistant")]
    assert "第 2 回合重試角色回覆" in arch[0]["reason"]
    fbs = {f["id"] for f in c.get(f"/api/feedback?run_id={a}").json()}
    assert keep_fb["id"] in fbs and gone_fb["id"] not in fbs


def test_manual_user_edit_and_batch_regenerate(c):
    d = start(c, [{"turns": 3, "seeds": 2}])
    a, b = d["runs"][0]["id"], d["runs"][1]["id"]
    before_b = msgs(c, b)

    # 手動改寫使用者發言 → 之後的角色回覆重新產生
    c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": b, "turn": 3, "role": "user"}], "user_text": "（我自己打的）妳還好嗎？", "later_users": "keep"})
    wait(c, d["id"])
    after = msgs(c, b)
    assert after[4]["content"] == "（我自己打的）妳還好嗎？" and after[4]["source"] == "manual"
    assert after[5]["content"] != before_b[5]["content"] and after[:4] == before_b[:4]
    assert c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": b, "turn": 3, "role": "user"}], "user_text": "  "}).status_code == 422

    # 批次：本階段所有分支第 2 回合的使用者發言重新產生，之後也重新產生
    before = {rid: msgs(c, rid) for rid in (a, b)}
    r = c.post(f"/api/experiments/{d['id']}/retry", json={"stage": {"stage": 0, "turn": 2, "role": "user"}, "later_users": "regenerate"}).json()
    assert sorted(r["started"]) == sorted([a, b])
    wait(c, d["id"])
    for rid in (a, b):
        now = msgs(c, rid)
        assert now[:2] == before[rid][:2]
        assert now[2]["source"] == "llm_user" and now[2]["content"] != before[rid][2]["content"]
        assert now[4]["content"] != before[rid][4]["content"]  # 第 3 回合的使用者發言也重新產生
        assert len(now) == 6

    # 沒有該回合 → 422
    r = c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": a, "turn": 9, "role": "assistant"}]})
    assert r.status_code == 422 and "沒有第 9 回合" in r.json()["detail"]


def test_promoted_branch_blocked_and_manual_next_stage(c):
    d = start(c, [{"turns": 1, "seeds": 2, "promote": {"mode": "manual"}}, {"turns": 3, "seeds": 1}])
    a = d["runs"][0]["id"]
    # 晉級時預先寫下一階段第 2 回合的使用者發言；第 3 回合留空 → LLM 扮演
    c.post(f"/api/experiments/{d['id']}/advance", json={"run_ids": [a], "user_turns": {"2": "我來說第二句", "3": "  "}})
    d = wait(c, d["id"])
    child = next(r for r in d["runs"] if r["stage"] == 1)
    m = msgs(c, child["id"])
    users = [(x["turn"], x["source"], x["content"]) for x in m if x["role"] == "user"]
    assert users[0] == (2, "manual", "我來說第二句") and users[1][1] == "llm_user"

    # 已有子分支的 run 重試 → 子分支連帶封存，上層跑完後以新對話重建並重跑
    old_child = msgs(c, child["id"])
    r = c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": a, "turn": 1, "role": "assistant"}],
                                                         "later_users": "keep", "tags": ["文風不佳"], "note": "太花俏"}).json()
    assert r["started"] == [a] and r["cascade"] == [child["id"]]
    d = wait(c, d["id"])
    assert d["status"] == "done"  # 重試完回到原本的狀態
    new_parent = msgs(c, a)
    new_child_run = c.get(f"/api/runs/{child['id']}").json()
    assert new_child_run["status"] == "done"
    # 子分支的歷史換成新的上層對話；使用者發言沿用（keep）
    assert new_child_run["scenario"]["history"][-1]["content"] == new_parent[-1]["content"]
    new_child = new_child_run["messages"]
    assert [(m["turn"], m["source"], m["content"]) for m in new_child if m["role"] == "user"] ==            [(m["turn"], m["source"], m["content"]) for m in old_child if m["role"] == "user"]
    assert [m["content"] for m in new_child if m["role"] == "assistant"] != [m["content"] for m in old_child if m["role"] == "assistant"]
    arch = c.get(f"/api/runs/{child['id']}/archive").json()
    assert arch and all(x["reason"] == "上層分支重試，連帶重跑" for x in arch)
    child = new_child_run

    # 子分支重試第 2 回合使用者發言（手動輸入在 advance 時寫過，重試不帶文字 → 改由 LLM 產生）
    c.post(f"/api/experiments/{d['id']}/retry", json={"items": [{"run_id": child["id"], "turn": 2, "role": "user"}]})
    wait(c, d["id"])
    m2 = msgs(c, child["id"])
    assert m2[0]["turn"] == 2 and m2[0]["source"] == "llm_user"
