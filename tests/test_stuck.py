"""卡住的分支：保活註解不斷、內容不來 → 逾時判定；單獨中止；其他分支執行中也能重試；續跑。"""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from prompt_tester import llm
from prompt_tester.app import create_app
from prompt_tester.config import Settings

from fake_llm import openrouter_stream


class Transport(httpx.AsyncBaseTransport):
    """seed 在 hang_seeds 裡的角色請求會一直送保活註解、不送內容。"""

    def __init__(self):
        self.hang_seeds: set[int] = set()
        self.n = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.n += 1
        if "請寫出" not in body["messages"][-1]["content"] and body.get("seed") in self.hang_seeds:
            async def keepalive():
                while True:
                    yield b": OPENROUTER PROCESSING\n\n"
                    await asyncio.sleep(0.05)
            return httpx.Response(200, content=keepalive(), headers={"content-type": "text/event-stream"})
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=openrouter_stream(f"回覆{self.n}", "", reasoning=""),
                              headers={"content-type": "text/event-stream"})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    t = Transport()
    app = create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"), http_transport=t)
    with TestClient(app) as c:
        yield c, t, monkeypatch


def status(c, eid):
    return c.get(f"/api/experiments/{eid}").json()


def until(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("條件沒有成立")


def start(c, seeds=3, turns=1):
    ws = c.get("/api/workspace").json()
    sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["嗨", "再一句"]}).json()["id"]
    cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
    c.put("/api/user-sim", json={"connection_id": cid})
    script = c.post("/api/scripts", json={"name": "r", "data": {"concurrency": 3, "stages": [{"turns": turns, "seeds": seeds}]}}).json()
    return c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()["id"]


def test_stall_detected(setup, monkeypatch):
    c, t, mp = setup
    mp.setattr(llm, "STALL_SECONDS", 0.4)
    t.hang_seeds = {2}  # seed 1 第 1 回合 → request seed = 2
    eid = start(c, seeds=2)
    d = until(lambda: (x := status(c, eid))["status"] != "running" and x)
    stuck = next(r for r in d["runs"] if r["seed"] == 1)
    assert stuck["status"] == "error" and "沒有收到內容" in stuck["error"]


def test_cancel_one_branch_retry_others_and_resume(setup):
    c, t, _ = setup
    t.hang_seeds = {2}
    eid = start(c, seeds=3)
    d = until(lambda: (x := status(c, eid)) and sum(r["status"] == "done" for r in x["runs"]) == 2 and x)
    assert d["status"] == "running"
    stuck = next(r for r in d["runs"] if r["seed"] == 1)
    other = next(r for r in d["runs"] if r["seed"] == 2)
    assert stuck["status"] == "running"

    # 其他分支還在跑時，已完成的分支可以重試；執行中的那條不行
    r = c.post(f"/api/experiments/{eid}/retry", json={"items": [{"run_id": other["id"], "turn": 1, "role": "assistant"},
                                                                 {"run_id": stuck["id"], "turn": 1, "role": "assistant"}]}).json()
    assert r["started"] == [other["id"]] and "執行中" in r["skipped"][0]["reason"]
    until(lambda: next(x for x in status(c, eid)["runs"] if x["id"] == other["id"])["status"] == "done")
    assert status(c, eid)["status"] == "running"  # 卡住的那條還在

    # 單獨中止卡住的分支 → 實驗回到等待 / 完成
    assert c.post(f"/api/experiments/{eid}/runs/{stuck['id']}/cancel").json()["status"] == "cancelled"
    d = until(lambda: (x := status(c, eid))["status"] != "running" and x)
    assert d["status"] == "done"
    s = next(r for r in d["runs"] if r["id"] == stuck["id"])
    assert s["status"] == "cancelled" and s["replies"] == 0
    assert c.post(f"/api/experiments/{eid}/runs/{stuck['id']}/cancel").status_code == 422

    # 續跑本階段未完成的分支（只有使用者發言 → 接著產生角色回覆，不重產使用者發言）
    t.hang_seeds = set()
    msgs_before = c.get(f"/api/runs/{stuck['id']}").json()["messages"]
    r = c.post(f"/api/experiments/{eid}/retry", json={"resume_stage": 0}).json()
    assert r["started"] == [stuck["id"]]
    d = until(lambda: (x := status(c, eid))["status"] != "running" and x)
    msgs = c.get(f"/api/runs/{stuck['id']}").json()["messages"]
    assert msgs[0] == msgs_before[0] and [(m["turn"], m["role"]) for m in msgs] == [(1, "user"), (1, "assistant")]
    assert next(r for r in d["runs"] if r["id"] == stuck["id"])["status"] == "done"
    # 已完成的再續跑 → 422（沒有可續跑的回合）
    r = c.post(f"/api/experiments/{eid}/retry", json={"items": [{"run_id": stuck["id"]}]})
    assert r.status_code == 422 and "已經跑完" in r.json()["detail"]


def test_resume_after_error_and_mid_run(setup, monkeypatch):
    c, t, mp = setup
    mp.setattr(llm, "STALL_SECONDS", 0.3)
    t.hang_seeds = {1 + 2}  # 第 2 回合的角色回覆卡住
    eid = start(c, seeds=1, turns=3)
    d = until(lambda: (x := status(c, eid))["status"] != "running" and x)
    run = d["runs"][0]
    assert run["status"] == "error"
    before = c.get(f"/api/runs/{run['id']}").json()["messages"]
    assert [(m["turn"], m["role"], bool(m["error"])) for m in before][-1] == (2, "assistant", True)
    t.hang_seeds = set()
    c.post(f"/api/experiments/{eid}/retry", json={"items": [{"run_id": run["id"]}]})
    until(lambda: status(c, eid)["status"] != "running")
    after = c.get(f"/api/runs/{run['id']}").json()["messages"]
    assert [(m["turn"], m["role"]) for m in after] == [(1, "user"), (1, "assistant"), (2, "user"), (2, "assistant"), (3, "user"), (3, "assistant")]
    assert after[:3] == before[:3] and not after[3]["error"]
