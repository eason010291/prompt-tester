import json

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings
from prompt_tester.runner import clean_user_output, strip_markup

from fake_llm import FakeLLM, openrouter_stream


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    return tmp_path


def reply(text: str) -> tuple[int, bytes]:
    return 200, openrouter_stream(text, "", reasoning="")


def client(tmp_path, fake):
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"),
                                 http_transport=fake.transport))


def events(resp) -> list[dict]:
    return [json.loads(x[5:]) for x in resp.text.splitlines() if x.startswith("data:")]


def setup(c, *, user_sim=True, first_line="嗨，好久不見。"):
    ws = c.get("/api/workspace").json()
    sid = c.post("/api/scenarios", json={
        "name": "測試", "char": "露", "user": "藍", "opening": "（露抬起頭）你來了。",
        "persona": "藍是剛下班的上班族", "user_goal": "約露週末去看展", "user_turns": [first_line],
    }).json()["id"]
    char_id = c.post("/api/connections", json={"name": "角色", "model": "char-model"}).json()["id"]
    user_id = c.post("/api/connections", json={"name": "模擬使用者", "model": "user-model"}).json()["id"]
    if user_sim:
        c.put("/api/user-sim", json={"connection_id": user_id})
    return ws["preset"], sid, char_id


def test_strip_markup_and_clean_output():
    text = "<details><summary>🧠</summary>思考</details>\n<pre class=\"gamestate\">[狀態]</pre>\n<p>她笑了。</p><style>.a{}</style>"
    assert strip_markup(text) == "[狀態]\n她笑了。"
    assert clean_user_output("藍：好啊，一起去吧。", "藍") == "好啊，一起去吧。"


def test_live_run_uses_scenario_first_line_then_llm_user(env):
    fake = FakeLLM(
        reply("<details>思考鏈</details><p>露：「週末？可以呀。」</p>"),  # 第 1 回合角色
        reply("藍：那就星期六下午兩點？"),  # 第 2 回合模擬使用者
        reply("露：「好，星期六下午兩點，我會準時在美術館門口等你。」"),  # 第 2 回合角色
    )
    with client(env, fake) as c:
        preset, sid, char_id = setup(c)
        ev = events(c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id, "turns": 2}))
        run = ev[-1]["run"]

    assert run["status"] == "done" and run["mode"] == "live"
    assert [(m["role"], m["source"]) for m in run["messages"]] == [
        ("user", "scenario"), ("assistant", "char"), ("user", "llm_user"), ("assistant", "char"),
    ]
    assert run["messages"][2]["content"] == "那就星期六下午兩點？"  # 去掉「藍：」前綴
    sim_req = fake.requests[1]
    assert sim_req["model"] == "user-model" and sim_req["max_tokens"] == 1500
    system, prompt = sim_req["messages"][0]["content"], sim_req["messages"][1]["content"]
    assert "藍是剛下班的上班族" in system and "約露週末去看展" in system
    assert "思考鏈" not in prompt and "<p>" not in prompt and "露：露：「週末？可以呀。」" in prompt
    char_req = fake.requests[2]
    assert [m["content"] for m in char_req["messages"] if m["role"] == "user"][-1] == "那就星期六下午兩點？"
    assert run["cost"] == pytest.approx(0.000021 * 3)
    assert run["checks"]["issues"] == []


def test_replay_reuses_user_lines_and_falls_back(env):
    fake = FakeLLM(reply("好"), reply("藍：週六好嗎"), reply("可以"),  # 第一次：live 2 回合
                   reply("好喔"), reply("可以喔"),  # 回放 2 回合：只有角色呼叫
                   reply("嗯"), reply("嗯嗯"), reply("藍：那就這樣"), reply("好"))  # 回放 3 回合：第 3 回合改由 LLM
    with client(env, fake) as c:
        preset, sid, char_id = setup(c)
        base = events(c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id, "turns": 2}))[-1]["run"]
        n_before = len(fake.requests)
        rep = events(c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id,
                                                 "turns": 2, "mode": "replay", "replay_of": base["id"]}))[-1]["run"]
        assert len(fake.requests) - n_before == 2
        assert [m["source"] for m in rep["messages"] if m["role"] == "user"] == ["replay", "replay"]
        assert [m["content"] for m in rep["messages"] if m["role"] == "user"] == ["嗨，好久不見。", "週六好嗎"]

        more = events(c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id,
                                                  "turns": 3, "mode": "replay", "replay_of": base["id"]}))[-1]["run"]
        assert [m["source"] for m in more["messages"] if m["role"] == "user"] == ["replay", "replay", "replay_fallback"]
        listed = c.get(f"/api/runs?scenario_id={sid}").json()
        assert [r["id"] for r in listed] == [more["id"], rep["id"], base["id"]] and listed[0]["replies"] == 3


def test_preflight_flags_refusal_and_truncation(env):
    truncated = openrouter_stream("", "", reasoning="想很久").replace(b'"finish_reason": "stop"', b'"finish_reason": "length"')
    fake = FakeLLM(reply("這個開場我需要先暫停一下，我不能在這個框架裡繼續。"), reply("藍：拜託"), (200, truncated))
    with client(env, fake) as c:
        preset, sid, char_id = setup(c)
        run = events(c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id, "turns": 2}))[-1]["run"]
    issues = [(i["turn"], i["message"]) for i in run["checks"]["issues"]]
    assert any(t == 1 and "出戲" in m for t, m in issues)
    assert any(t == 2 and "沒有產生內容" in m for t, m in issues)


def test_requires_user_sim_connection_for_live(env):
    with client(env, FakeLLM()) as c:
        preset, sid, char_id = setup(c, user_sim=False)
        r = c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id, "turns": 2})
        assert r.status_code == 422 and "模擬使用者" in r.json()["detail"]


def test_char_error_stops_run(env):
    fake = FakeLLM((401, b'{"error":{"message":"bad key"}}'))
    with client(env, fake) as c:
        preset, sid, char_id = setup(c)
        run = events(c.post("/api/runs", json={"preset": preset, "scenario_id": sid, "connection_id": char_id, "turns": 3}))[-1]["run"]
    assert run["status"] == "error" and "bad key" in run["error"] and len(run["messages"]) == 2
