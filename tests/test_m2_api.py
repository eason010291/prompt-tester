import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prompt_tester import db
from prompt_tester.app import create_app
from prompt_tester.config import Settings

from fake_llm import FakeLLM, openrouter_stream


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return tmp_path


def client_for(tmp_path: Path, fake: FakeLLM | None = None) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist")
    return TestClient(create_app(settings, http_transport=(fake or FakeLLM()).transport))


def read_sse(resp) -> list[dict]:
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


def test_workspace_starts_with_scenario(env):
    with client_for(env) as c:
        ws = c.get("/api/workspace").json()
        sid = ws["context"]["scenario_id"]
        sc = c.get(f"/api/scenarios/{sid}").json()
        assert sc["user_turns"] and ws["context"]["user_input"] == sc["user_turns"][0]
        res = c.post("/api/build", json={"preset": ws["preset"], "context": ws["context"]}).json()
        roles = [m["role"] for m in res["messages"]]
        assert "assistant" in roles and res["messages"][-2]["content"] == sc["user_turns"][0] or \
            any(m["content"] == sc["user_turns"][0] for m in res["messages"])


def test_legacy_workspace_context_migrates_to_scenario(env):
    data = env / "data"
    conn = db.connect(data / "prompt_tester.db")
    db.migrate(conn)
    legacy = {"char": "聿", "user": "藍", "seed": 7,
              "history": [{"role": "assistant", "content": "開場"}, {"role": "user", "content": "嗨"}]}
    with conn:
        conn.execute("INSERT INTO workspaces VALUES (1, ?, ?, 'x')",
                     (json.dumps({"name": "p", "blocks": []}), json.dumps(legacy)))
    conn.close()

    with client_for(env) as c:
        # 啟動時就轉換完成：不必先讀工作區，情境頁就看得到
        assert [s["name"] for s in c.get("/api/scenarios").json()] == ["預覽情境（自 M1 轉入）"]
        ws = c.get("/api/workspace").json()
        assert ws["context"]["seed"] == 7
        sc = c.get(f"/api/scenarios/{ws['context']['scenario_id']}").json()
        assert (sc["char"], sc["user"], sc["opening"]) == ("聿", "藍", "開場")
        assert sc["history"] == [{"role": "user", "content": "嗨", "name": None}]
        # 第二次讀取不再重複轉換
        assert c.get("/api/workspace").json()["context"]["scenario_id"] == sc["id"]
        assert len(c.get("/api/scenarios").json()) == 1


def test_scenario_and_connection_crud(env):
    with client_for(env) as c:
        s = c.post("/api/scenarios", json={"name": "放學", "char": "聿", "vars": {"地點": "教室"}}).json()
        s["opening"] = "（放學鈴響）"
        assert c.put(f"/api/scenarios/{s['id']}", json=s).json()["opening"] == "（放學鈴響）"
        assert c.delete(f"/api/scenarios/{s['id']}").status_code == 204
        assert c.get(f"/api/scenarios/{s['id']}").status_code == 404

        conn = c.post("/api/connections", json={"name": "GLM", "model": "z-ai/glm-5.3-flash",
                                                 "routing": {"quantizations": ["fp8"], "allow_fallbacks": False}}).json()
        assert c.get("/api/connections").json()[0]["routing"]["quantizations"] == ["fp8"]
        assert c.delete(f"/api/connections/{conn['id']}").status_code == 204


def test_provider_key_saved_to_config_not_exposed(env):
    with client_for(env) as c:
        p = c.get("/api/providers").json()[0]
        assert p["has_key"] and p["key_hint"] == "…1234" and "sk-test" not in json.dumps(p)
        c.put("/api/providers/openrouter", json={"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-new-9999"})
        assert c.get("/api/providers").json()[0]["key_hint"] == "…9999"
    assert 'api_key = "sk-new-9999"' in (env / "config.local.toml").read_text("utf-8")


def test_generate_streams_and_records(env):
    fake = FakeLLM((200, openrouter_stream()))
    with client_for(env, fake) as c:
        ws = c.get("/api/workspace").json()
        cid = c.post("/api/connections", json={
            "name": "GLM", "model": "z-ai/glm-5.3-flash", "reasoning_effort": "low",
            "routing": {"order": ["z-ai"], "allow_fallbacks": False, "quantizations": ["fp8"]},
        }).json()["id"]
        resp = c.post("/api/generate", json={"preset": ws["preset"], "context": ws["context"], "connection_id": cid})
        events = read_sse(resp)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "content") == "你好，世界"

    sent = fake.requests[0]
    assert sent["model"] == "z-ai/glm-5.3-flash" and sent["seed"] == ws["context"]["seed"]
    assert sent["provider"] == {"order": ["z-ai"], "quantizations": ["fp8"], "allow_fallbacks": False}
    assert sent["reasoning"] == {"effort": "low"}
    # 範例預設在聊天歷史之後還有「輸出格式」區塊，使用者輸入是最後一則 user
    assert [m for m in sent["messages"] if m["role"] == "user"][-1]["content"] == ws["context"]["user_input"]

    g = events[-1]["generation"]
    assert g["status"] == "done" and g["output"] == "你好，世界" and g["reasoning"] == "先想想"
    assert g["cost"] == 0.000021 and g["cost_source"] == "api" and g["served_by"] == "Z.AI"
    assert g["usage"]["prompt_tokens"] == 120 and g["request"]["model"] == "z-ai/glm-5.3-flash"
    assert "sk-test" not in json.dumps(g)


def test_generate_error_recorded(env):
    fake = FakeLLM((401, b'{"error":{"message":"No auth credentials found"}}'))
    with client_for(env, fake) as c:
        ws = c.get("/api/workspace").json()
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        events = read_sse(c.post("/api/generate", json={"preset": ws["preset"], "context": ws["context"], "connection_id": cid}))
        assert events[-1]["type"] == "error" and "No auth" in events[-1]["message"]
        assert c.get("/api/generations").json()[0]["status"] == "error"


def test_generate_requires_key_and_model(env, monkeypatch):
    (env / "config.local.toml").write_text("", "utf-8")
    with client_for(env) as c:
        ws = c.get("/api/workspace").json()
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        r = c.post("/api/generate", json={"preset": ws["preset"], "context": ws["context"], "connection_id": cid})
        assert r.status_code == 422 and "API key" in r.json()["detail"]


def test_models_catalog(env):
    with client_for(env) as c:
        models = c.get("/api/providers/openrouter/models").json()
        assert models[0]["pricing"] == {"prompt": 0.15, "completion": 0.5, "cache_read": None}
