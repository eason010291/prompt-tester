import time

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings
from prompt_tester.metrics import DEFAULT_METRICS, measure

from fake_llm import FakeLLM, openrouter_stream


@pytest.mark.parametrize(
    "text, n",
    [
        ("她不是生氣，而是害怕。", 1),
        ("這並不是巧合而是安排。並非如此，而是那樣", 2),
        ("不是因為你，也不是因為她，而是因為我", 1),  # 同一句只算一次
        ("他說不是。然後而是什麼", 0),  # 跨句不算
        ("<details>不是A而是B</details>外面不是C而是D", 1),  # 只數正文
        ("<!-- 不是A而是B -->沒有", 0),
    ],
)
def test_not_but(text, n):
    assert measure(text, DEFAULT_METRICS) == {"not_but": n}


def test_metrics_in_api(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    replies = iter(["她不是生氣，而是害怕。也不是累，而是怕。", "她笑了。"])

    def responder(body):
        return 200, openrouter_stream(next(replies), "", reasoning="")

    with TestClient(create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"),
                               http_transport=FakeLLM(default=responder).transport)) as c:
        assert [m["id"] for m in c.get("/api/metrics").json()] == ["not_but"]
        ws = c.get("/api/workspace").json()
        sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["嗨"]}).json()["id"]
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        script = c.post("/api/scripts", json={"name": "r", "data": {"concurrency": 1, "stages": [{"turns": 1, "seeds": 2}]}}).json()
        eid = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()["id"]
        end = time.time() + 10
        while (d := c.get(f"/api/experiments/{eid}").json())["status"] == "running" and time.time() < end:
            time.sleep(0.05)
        assert d["metric_defs"][0]["name"] == "不是…而是…"
        counts = sorted(r["metrics"]["not_but"] for r in d["runs"])
        assert counts == [0, 2]
        a, b = d["runs"]
        run = c.get(f"/api/runs/{a['id']}").json()
        assert run["metrics"]["totals"]["not_but"] == a["metrics"]["not_but"]
        cmp = c.get(f"/api/compare/runs?a={a['id']}&b={b['id']}").json()
        assert {cmp["a"]["metrics"]["totals"]["not_but"], cmp["b"]["metrics"]["totals"]["not_but"]} == {0, 2}
