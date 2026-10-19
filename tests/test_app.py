from pathlib import Path

from fastapi.testclient import TestClient

from prompt_tester import db
from prompt_tester.app import create_app
from prompt_tester.config import Settings, load_settings


def make_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist", **kw)


def test_health_reports_schema_version(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["schema_version"] == db.MIGRATIONS[-1][0]


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.migrate(conn) == db.migrate(conn) == db.MIGRATIONS[-1][0]


def test_serves_built_frontend_with_spa_fallback(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")

    with TestClient(create_app(make_settings(tmp_path))) as client:
        assert client.get("/").text == "<html>app</html>"
        assert client.get("/experiments/42").text == "<html>app</html>"
        assert client.get("/favicon.svg").text == "<svg/>"
        assert client.get("/../config.local.toml").text == "<html>app</html>"


def test_load_settings_reads_providers(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[server]\nport = 9000\ndata_dir = "d"\n'
        '[providers.openrouter]\nbase_url = "https://x/api/v1"\napi_key = "k"\n',
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.port == 9000
    assert s.data_dir.name == "d" and s.data_dir.is_absolute()
    assert s.providers["openrouter"].api_key == "k"


def test_workspace_roundtrip_and_build(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        ws = client.get("/api/workspace").json()
        assert ws["updated_at"] is None and ws["preset"]["blocks"]

        ws["preset"]["name"] = "改名"
        saved = client.put("/api/workspace", json=ws).json()
        assert saved["updated_at"]
        assert client.get("/api/workspace").json()["preset"]["name"] == "改名"

        res = client.post("/api/build", json={"preset": ws["preset"], "context": ws["context"]}).json()
        assert res["messages"] and res["total_tokens"] > 0
        assert "{{char}}" not in res["messages"][0]["content"]

        raw = client.post(
            "/api/build", json={"preset": ws["preset"], "context": ws["context"], "expand_macros": False}
        ).json()
        assert "{{char}}" in raw["messages"][0]["content"]
