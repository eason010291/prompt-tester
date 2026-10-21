import json

import pytest
from fastapi.testclient import TestClient

from prompt_tester import db, vcs
from prompt_tester.app import create_app
from prompt_tester.config import Settings
from prompt_tester.preset import Block, Preset

from fake_llm import FakeLLM, openrouter_stream


def blk(i, content="", **kw):
    return Block(id=i, name=i, content=content or f"內容{i}", **kw)


def p(*blocks, name="預設"):
    return Preset(name=name, blocks=list(blocks))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    return c


# ---- 物件層 ----

def test_tree_id_ignores_collapsed_but_not_enabled(conn):
    a = p(blk("x"), blk("y"))
    b = a.model_copy(deep=True)
    b.blocks[0].collapsed = True
    assert vcs.tree_id_of(a) == vcs.tree_id_of(b)
    b.blocks[0].enabled = False
    assert vcs.tree_id_of(a) != vcs.tree_id_of(b)


def test_blobs_deduplicated_and_toggle_adds_no_blob(conn):
    a = p(blk("x"), blk("y"))
    vcs.create_commit(conn, a, [], "一")
    n_blobs = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    b = a.model_copy(deep=True)
    b.blocks[1].enabled = False
    vcs.create_commit(conn, b, [], "二")
    assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == n_blobs
    assert conn.execute("SELECT COUNT(*) FROM trees").fetchone()[0] == 2


def test_read_tree_round_trip(conn):
    a = p(blk("x", group="g", injection_position=1), blk("y", enabled=False), name="聿")
    a.extra = {"extensions": {"regex_scripts": [{"scriptName": "CSS"}]}}
    c = vcs.create_commit(conn, a, [], "一")
    back = vcs.read_tree(conn, c.tree_id)
    strip = lambda pr: [b.model_dump(exclude={"collapsed"}) for b in pr.blocks]  # noqa: E731
    assert strip(back) == strip(a) and back.extra == a.extra and back.name == "聿"


def test_diff_detects_all_kinds():
    a = p(blk("a"), blk("b"), blk("c"), blk("d"))
    b = p(blk("c"), blk("a", "改過"), blk("b", enabled=False), blk("e"))
    d = vcs.diff_presets(a, b)
    by_id = {e["id"]: e for e in d["blocks"]}
    assert by_id["e"]["status"] == ["added"]
    assert by_id["d"]["status"] == ["removed"]
    assert by_id["a"]["status"] == ["modified"] and by_id["a"]["changes"]["content"] == ["內容a", "改過"]
    assert by_id["b"]["status"] == ["toggled"]
    assert "moved" in by_id["c"]["status"]
    assert d["summary"] == {"added": 1, "removed": 1, "modified": 1, "toggled": 1, "moved": 1}
    # 刪除的 d 放在它原本前一個區塊（c）之後
    order = [e["id"] for e in d["blocks"]]
    assert order.index("d") == order.index("c") + 1


# ---- API ----

def client(tmp_path, fake=None):
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "dist"),
                                 http_transport=(fake or FakeLLM()).transport))


def edit(c, fn):
    ws = c.get("/api/workspace").json()
    fn(ws["preset"])
    c.put("/api/workspace", json=ws)
    return ws


def test_commit_flow_and_dirty_state(tmp_path):
    with client(tmp_path) as c:
        st = c.get("/api/vcs/state").json()
        assert st["branch"] == "main" and st["base_commit"] is None and st["dirty"]

        first = c.post("/api/vcs/commit", json={"message": "初版"}).json()
        st = c.get("/api/vcs/state").json()
        assert st["base_commit"]["id"] == first["id"] and not st["dirty"]
        main = next(r for r in c.get("/api/vcs/log").json()["refs"] if r["name"] == "main")
        assert main["commit_id"] == first["id"] and main["protected"]

        assert c.post("/api/vcs/commit", json={"message": "沒變"}).status_code == 422

        ws = edit(c, lambda pr: pr["blocks"][1].update(content="改了"))
        assert c.get("/api/vcs/state").json()["dirty"]
        second = c.post("/api/vcs/commit", json={"message": "第二版", "preset": ws["preset"]}).json()
        assert second["parents"] == [first["id"]]
        assert [x["id"] for x in c.get("/api/vcs/log").json()["commits"]] == [second["id"], first["id"]]


def test_checkout_guards_dirty_and_restores(tmp_path):
    with client(tmp_path) as c:
        first = c.post("/api/vcs/commit", json={"message": "一"}).json()
        edit(c, lambda pr: pr["blocks"][1].update(content="第二版內容"))
        second = c.post("/api/vcs/commit", json={"message": "二"}).json()
        edit(c, lambda pr: pr["blocks"][1].update(content="未提交"))

        r = c.post("/api/vcs/checkout", json={"target": first["id"][:8]})
        assert r.status_code == 409
        st = c.post("/api/vcs/checkout", json={"target": first["id"][:8], "force": True}).json()
        assert st["branch"] is None and st["base_commit"]["id"] == first["id"] and not st["dirty"]
        assert c.get("/api/workspace").json()["preset"]["blocks"][1]["content"] != "未提交"

        # detached 提交必須指定新分支
        edit(c, lambda pr: pr["blocks"][1].update(content="從舊版分岔"))
        assert c.post("/api/vcs/commit", json={"message": "分岔"}).status_code == 422
        fork = c.post("/api/vcs/commit", json={"message": "分岔", "branch": "實驗"}).json()
        assert fork["parents"] == [first["id"]]
        assert c.get("/api/vcs/state").json()["branch"] == "實驗"

        st = c.post("/api/vcs/checkout", json={"target": "main"}).json()
        assert st["branch"] == "main" and st["base_commit"]["id"] == second["id"]


def test_branch_from_here_keeps_working_changes(tmp_path):
    with client(tmp_path) as c:
        c.post("/api/vcs/commit", json={"message": "一"})
        edit(c, lambda pr: pr["blocks"][1].update(content="還沒提交"))
        c.post("/api/vcs/refs", json={"name": "試文風", "checkout": True})
        st = c.get("/api/vcs/state").json()
        assert st["branch"] == "試文風" and st["dirty"]
        assert c.get("/api/workspace").json()["preset"]["blocks"][1]["content"] == "還沒提交"


def test_tags_and_ref_protection(tmp_path):
    with client(tmp_path) as c:
        first = c.post("/api/vcs/commit", json={"message": "一"}).json()
        assert c.post("/api/vcs/refs", json={"name": "聿V1.2.3-0909", "kind": "tag"}).status_code == 200
        assert c.post("/api/vcs/refs", json={"name": "聿V1.2.3-0909", "kind": "tag"}).status_code == 422
        assert c.post("/api/vcs/refs", json={"name": "壞 名稱"}).status_code == 422
        assert c.delete("/api/vcs/refs/main").status_code == 422  # 受保護
        c.post("/api/vcs/refs", json={"name": "b2", "checkout": True})
        assert c.delete("/api/vcs/refs/b2").status_code == 422  # 目前所在分支
        assert c.delete("/api/vcs/refs/聿V1.2.3-0909").status_code == 204
        d = c.get(f"/api/vcs/diff?a=EMPTY&b={first['id']}").json()
        assert d["summary"]["added"] == len(c.get("/api/workspace").json()["preset"]["blocks"])


def test_generation_links_commit_when_preset_matches(tmp_path):
    fake = FakeLLM((200, openrouter_stream()), (200, openrouter_stream()))
    with client(tmp_path, fake) as c:
        cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
        first = c.post("/api/vcs/commit", json={"message": "一"}).json()
        ws = c.get("/api/workspace").json()
        body = {"preset": ws["preset"], "context": ws["context"], "connection_id": cid}
        events = [json.loads(x[5:]) for x in c.post("/api/generate", json=body).text.splitlines() if x.startswith("data:")]
        assert events[-1]["generation"]["commit_id"] == first["id"]

        body["preset"]["blocks"][1]["content"] = "未提交的改動"
        events = [json.loads(x[5:]) for x in c.post("/api/generate", json=body).text.splitlines() if x.startswith("data:")]
        assert events[-1]["generation"]["commit_id"] is None
        assert c.get("/api/vcs/log").json()["commits"][0]["generations"] == 1
