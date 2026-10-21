import json

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings
from prompt_tester.feedback import parse_scores

from fake_llm import FakeLLM, openrouter_stream


@pytest.mark.parametrize(
    "text, expected",
    [
        ("【評分】\n- 文風：7/10\n- 格式完整: 8.5 / 10\n角色一致：10/10", {"文風": 7, "格式完整": 8.5, "角色一致": 10}),
        ("- 屬性：_/10\n- 劇情推進：/10", {}),  # 佔位
        ("文風：11/10\n節奏：3/100", {}),  # 超出範圍、不是 /10
        ("• 【文風】：6/10", {"文風": 6}),
        ("說明：第 3 段 7/10 的地方", {}),  # 冒號後不是直接接分數
    ],
)
def test_parse_scores(text, expected):
    assert parse_scores(text) == expected


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    return tmp_path


def reply(text):
    return 200, openrouter_stream(text, "", reasoning="")


def run_once(c, preset, sid, cid):
    lines = c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": cid, "turns": 1}).text
    return [json.loads(x[5:]) for x in lines.splitlines() if x.startswith("data:")][-1]["run"]


def test_feedback_scores_compare_and_commit_average(env):
    long = "露：「好啊，週末一起去看展吧，我很久沒有出門走走了。」"
    fake = FakeLLM(reply(long), reply(long + "（第二版）"))
    with TestClient(create_app(Settings(data_dir=env / "data", web_dist=env / "dist"), http_transport=fake.transport)) as c:
        ws = c.get("/api/workspace").json()
        sid = c.post("/api/scenarios", json={"name": "s", "char": "露", "user": "藍", "user_turns": ["嗨"]}).json()["id"]
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        first = c.post("/api/vcs/commit", json={"message": "一"}).json()
        run_a = run_once(c, ws["preset"], sid, cid)

        preset_b = ws["preset"]
        preset_b["blocks"][1]["content"] += "（加強語氣）"
        c.put("/api/workspace", json={"preset": preset_b, "context": ws["context"]})
        run_b = run_once(c, preset_b, sid, cid)

        f1 = c.post("/api/feedback", json={"target_type": "run", "target_id": run_a["id"], "text": "文風：6/10\n節奏：8/10"}).json()
        assert f1["scores"] == {"文風": 6, "節奏": 8}
        msg_id = run_a["messages"][1]["id"]
        c.post("/api/feedback", json={"target_type": "run_message", "target_id": msg_id, "text": "文風：8/10\n太長了"})
        c.post("/api/feedback", json={"target_type": "run", "target_id": run_b["id"], "text": "文風：9/10"})

        edited = c.put(f"/api/feedback/{f1['id']}", json={"text": "文風：4/10\n節奏：8/10"}).json()
        assert edited["scores"]["文風"] == 4

        cmp = c.get(f"/api/compare/runs?a={run_a['id']}&b={run_b['id']}").json()
        assert cmp["a"]["scores"]["attributes"] == {"文風": 6, "節奏": 8}  # (4 + 8) / 2
        assert cmp["a"]["scores"]["overall"] == pytest.approx((4 + 8 + 8) / 3, abs=0.01)
        assert cmp["b"]["scores"]["overall"] == 9
        assert not cmp["same_preset"] and cmp["diff"]["summary"]["modified"] == 1
        assert cmp["b"]["preset"]["blocks"][1]["content"].endswith("（加強語氣）")
        assert [m["content"] for m in cmp["b"]["messages"] if m["role"] == "assistant"] == [long + "（第二版）"]

        runs = {r["id"]: r for r in c.get("/api/runs").json()}
        assert runs[run_b["id"]]["score"] == 9 and runs[run_b["id"]]["scenario_name"] == "s"
        log = {x["id"]: x for x in c.get("/api/vcs/log").json()["commits"]}
        assert log[first["id"]]["runs"] == 1 and log[first["id"]]["score"]["overall"] == pytest.approx(6.67, abs=0.01)

        assert c.post("/api/feedback", json={"target_type": "nope", "target_id": 1, "text": "x"}).status_code == 422
        c.delete(f"/api/runs/{run_a['id']}")
        assert c.get(f"/api/feedback?run_id={run_a['id']}").json() == []


def test_template_roundtrip(env):
    with TestClient(create_app(Settings(data_dir=env / "data", web_dist=env / "dist"))) as c:
        assert "_/10" in c.get("/api/feedback-template").json()["text"]
        c.put("/api/feedback-template", json={"text": "- 文風：_/10\n- 格式：_/10"})
        assert c.get("/api/feedback-template").json()["text"].startswith("- 文風")
        assert c.post("/api/feedback/parse", json={"text": "文風：7/10"}).json() == {"文風": 7}
