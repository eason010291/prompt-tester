import json
import time

import pytest
from fastapi.testclient import TestClient

from prompt_tester.app import create_app
from prompt_tester.config import Settings

from fake_llm import FakeLLM, openrouter_stream


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('[providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\napi_key = "sk-test-1234"\n', "utf-8")
    monkeypatch.setenv("PROMPT_TESTER_CONFIG", str(cfg))
    return tmp_path


def make(env, fake=None, **kw):
    return TestClient(create_app(Settings(data_dir=env / "data", web_dist=env / "dist", **kw),
                                 http_transport=fake.transport if fake else None))


def tool(c, name, action, args=None, **headers):
    return c.post(f"/api/agent/tools/{name}", json={"action": action, **(args or {})}, headers=headers)


def content_block(c):
    blocks = tool(c, "prompt", "read").json()["result"]["blocks"]
    return next(b for b in blocks if b.get("kind") is None and b["chars"] > 10)


def test_rest_tools_edit_and_commit(env):
    with make(env) as c:
        c.post("/api/vcs/commit", json={"message": "基準"})
        listed = tool(c, "prompt", "read").json()["result"]
        assert listed["branch"] == "main" and listed["uncommitted_changes"] is False
        b = content_block(c)
        full = tool(c, "prompt", "read", {"path": f"blocks[{b['id'][:8]}]"}).json()["result"]  # 前綴也可以
        assert full["id"] == b["id"] and full["content"]
        path = f"blocks[{b['id']}].content"

        # patch：找不到 / 不唯一 → 錯誤訊息給 LLM 修正
        r = tool(c, "prompt", "patch", {"path": path, "find": "不存在的字串", "replace": "x"})
        assert r.status_code == 422 and "找不到" in r.json()["detail"]
        assert "需要 path、find、replace" in tool(c, "prompt", "patch").json()["detail"]
        snippet = full["content"][:12]
        assert tool(c, "prompt", "patch", {"path": path, "find": snippet, "replace": snippet + "【改】"}).status_code == 200
        assert "【改】" in tool(c, "prompt", "read", {"path": path}).json()["result"]

        added = tool(c, "prompt", "add", {"value": {"name": "新規則", "content": "不要替使用者說話。"}, "after": b["id"]}).json()["result"]
        assert added["index"] == b["index"] + 1
        assert tool(c, "prompt", "move", {"path": f"blocks[{added['id']}]"}).json()["result"]["index"] == 0
        r = tool(c, "prompt", "set", {"path": f"blocks[{added['id']}].enabled", "value": False}).json()["result"]
        assert r["old"] is True and r["new"] is False
        marker = next(x for x in tool(c, "prompt", "read").json()["result"]["blocks"] if x.get("kind") == "marker")
        assert tool(c, "prompt", "remove", {"path": f"blocks[{marker['id']}]"}).status_code == 422
        assert "marker" in tool(c, "prompt", "set", {"path": f"blocks[{marker['id']}].content", "value": "x"}).json()["detail"]
        assert "沒有欄位" in tool(c, "prompt", "set", {"path": f"blocks[{b['id']}].contnet", "value": "x"}).json()["detail"]
        assert "不能修改" in tool(c, "prompt", "set", {"path": f"blocks[{b['id']}].id", "value": "x"}).json()["detail"]
        assert "不合法" in tool(c, "prompt", "set", {"path": f"blocks[{b['id']}].role", "value": "narrator"}).json()["detail"]
        assert tool(c, "prompt", "set", {"path": "params.temperature", "value": 0.7}).status_code == 200
        assert tool(c, "prompt", "read", {"path": "params.temperature"}).json()["result"] == 0.7
        assert tool(c, "prompt", "set", {"path": "params.temperature", "value": 5}).status_code == 422  # 範圍檢查

        prev = tool(c, "prompt", "preview", {"max_chars": 200}).json()["result"]
        assert prev["messages"] and any("新規則" in m["sources"] for m in prev["messages"]) is False  # 已停用
        diff = tool(c, "prompt", "diff").json()["result"]
        assert diff["summary"]["added"] == 1 and diff["summary"]["modified"] == 1

        # main 受保護：外部 agent 提交時自動建立 agent/ 分支，main 不動
        main_before = next(r for r in c.get("/api/vcs/log").json()["refs"] if r["name"] == "main")["commit_id"]
        done = tool(c, "prompt", "commit", {"message": "依評語修改"}, **{"X-Agent-Name": "claude-code"}).json()["result"]
        assert done["branch"].startswith("agent/")
        log = c.get("/api/vcs/log").json()
        assert next(r for r in log["refs"] if r["name"] == "main")["commit_id"] == main_before
        newest = log["commits"][0]
        assert newest["author"] == "agent:claude-code" and newest["meta"]["source"] == "rest"
        assert c.get("/api/vcs/state").json()["branch"] == done["branch"]
        # 在非受保護分支上再提交就直接前進
        tool(c, "prompt", "set", {"path": f"blocks[{added['id']}].enabled", "value": True})
        assert tool(c, "prompt", "commit", {"message": "再改"}).json()["result"]["branch"] == done["branch"]

        # 版本：log 與 diff
        vlog = tool(c, "versions", "log").json()["result"]
        assert vlog["commits"][0]["message"] == "再改" and vlog["head"]["branch"] == done["branch"]
        vd = tool(c, "versions", "diff", {"commit": vlog["commits"][1]["id"]}).json()["result"]
        assert vd["summary"]["added"] == 1

        # 參數錯誤、未知工具、未知動作
        assert tool(c, "prompt", "read", {"path": "blocks[zzzzzzzz]"}).status_code == 422
        assert c.post("/api/agent/tools/prompt", json={}).status_code == 422
        assert tool(c, "prompt", "explode").status_code == 422
        assert c.post("/api/agent/tools/patch_block", json={}).status_code == 404


def test_all_params_required_with_empty_sentinels(env):
    """schema 把每個參數都標為必填（較弱的模型會略過選填參數）；用不到的填空值，伺服器視為沒給。"""
    with make(env) as c:
        schema = next(t for t in c.get("/api/agent/tools").json() if t["name"] == "prompt")["input_schema"]
        assert set(schema["required"]) == set(schema["properties"])
        assert all("anyOf" not in p for p in schema["properties"].values())
        filler = {"path": "", "value": "null", "find": "", "replace": "", "after": "", "scenario_id": 0, "max_chars": 0, "message": ""}
        overview = tool(c, "prompt", "read", filler).json()["result"]
        assert "blocks" in overview
        b = content_block(c)
        assert tool(c, "prompt", "read", filler | {"path": f"blocks[{b['id']}].name"}).json()["result"] == b["name"]
        prev = tool(c, "prompt", "preview", filler).json()["result"]
        assert prev["messages"]  # max_chars 0 → 預設值，不會截成空字串
        assert all(m["content"] for m in prev["messages"])
        # replace 的空字串有意義（刪除）
        full = tool(c, "prompt", "read", {"path": f"blocks[{b['id']}].content"}).json()["result"]
        r = tool(c, "prompt", "patch", filler | {"path": f"blocks[{b['id']}].content", "find": full[:5]})
        assert r.status_code == 200, r.text
        assert tool(c, "prompt", "read", {"path": f"blocks[{b['id']}].content"}).json()["result"] == full[5:]
        # 情境的 index：-1 = 加在最後，0 = 最前面
        sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["一", "二"]}).json()["id"]
        sc_filler = {"path": "user_turns", "value": "三", "find": "", "replace": "", "index": -1, "message": ""}
        assert c.post("/api/agent/tools/scenario", json={"action": "add", "scenario_id": sid, **sc_filler}).json()["result"]["index"] == 2
        assert c.post("/api/agent/tools/scenario", json={"action": "add", "scenario_id": sid, **sc_filler, "value": "零", "index": 0}).json()["result"]["index"] == 0
        assert c.get(f"/api/scenarios/{sid}").json()["user_turns"] == ["零", "一", "二", "三"]


def test_gates_and_token(env):
    with make(env, agent_token="s3cret") as c:
        assert c.get("/api/agent/tools").status_code == 403
        h = {"X-Agent-Token": "s3cret"}
        tools = {t["name"]: t for t in c.get("/api/agent/tools", headers=h).json()}
        assert set(tools) == {"prompt", "scenario", "experiment", "script", "versions"}
        run = next(a for a in tools["experiment"]["actions"] if a["name"] == "run")
        assert run["enabled"] is False and run["builtin"] is False
        assert "run" not in tools["experiment"]["input_schema"]["properties"]["action"]["enum"]
        assert "path" in tools["prompt"]["input_schema"]["properties"]
        r = c.post("/api/agent/tools/experiment", json={"action": "run", "experiment_id": 1}, headers=h)
        assert r.status_code == 422 and "未開啟" in r.json()["detail"]
        s = c.get("/api/agent/settings").json()
        c.put("/api/agent/settings", json=s | {"allow_params": False, "system_prompt": "新的提示詞"})
        r = c.post("/api/agent/tools/prompt", json={"action": "set", "path": "params.temperature", "value": 1}, headers=h)
        assert r.status_code == 422 and "取樣參數不開放" in r.json()["detail"]
        # 系統提示詞改版時保留舊版
        s2 = c.get("/api/agent/settings").json()
        assert s2["system_prompt"] == "新的提示詞" and s2["prompt_history"][0]["text"] == s["system_prompt"]
        assert c.post("/mcp", json={}).status_code == 403


def test_legacy_default_prompt_upgraded(env):
    from prompt_tester import agent_tools as at
    from prompt_tester import store

    with make(env) as c:
        store.set_setting(c.app.state.db, at.AGENT_KEY, {"system_prompt": at._LEGACY_PROMPTS[0], "allow_opening": True})
        s = c.get("/api/agent/settings").json()
        assert s["system_prompt"] == at.DEFAULT_SYSTEM_PROMPT and s["allow_fixtures"] is True and "allow_opening" not in s


def test_mcp_streamable_http(env):
    with make(env) as c:
        c.post("/api/vcs/commit", json={"message": "基準"})
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

        def rpc(method, params=None, rid=1):
            r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            assert r.status_code == 200, r.text
            return r.json()

        init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "pytest-client", "version": "1"}})
        assert init["result"]["serverInfo"]["name"] == "prompt-tester"
        listed = {t["name"]: t for t in rpc("tools/list", rid=2)["result"]["tools"]}
        assert set(listed) == {"prompt", "scenario", "experiment", "script", "versions"}
        assert "run" not in listed["experiment"]["inputSchema"]["properties"]["action"]["enum"]
        assert "- patch：" in listed["prompt"]["description"]
        res = rpc("tools/call", {"name": "prompt", "arguments": {"action": "read"}}, rid=3)["result"]
        assert not res.get("isError") and json.loads(res["content"][0]["text"])["branch"] == "main"
        bad = rpc("tools/call", {"name": "prompt", "arguments": {"action": "read", "path": "blocks[zzzzzzzz]"}}, rid=4)["result"]
        assert bad["isError"] and "找不到 id=zzzzzzzz" in bad["content"][0]["text"]


# ---- ① 內建迴圈 ----

def completion(content="", tool_calls=None, cost=0.001):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
            for i, (n, a) in enumerate(tool_calls)
        ]
    return {"id": "gen-x", "model": "m", "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": cost}}


class Script:
    """角色 / 模擬使用者走串流；修改者走非串流，依序回傳排好的回應。"""

    def __init__(self, plan):
        self.plan = list(plan)
        self.modifier_requests = []

    def __call__(self, body):
        if body.get("stream") is False:
            self.modifier_requests.append(body)
            step = self.plan.pop(0)
            return 200, json.dumps(step(body) if callable(step) else step, ensure_ascii=False).encode()
        return 200, openrouter_stream("角色回覆", "", reasoning="")


def wait_round(c, rid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/agent/rounds/{rid}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("修改沒有結束")


def setup_experiment(c):
    ws = c.get("/api/workspace").json()
    c.post("/api/vcs/commit", json={"message": "C0"})
    sid = c.post("/api/scenarios", json={"name": "s", "user_turns": ["嗨"]}).json()["id"]
    cid = c.post("/api/connections", json={"name": "x", "model": "m"}).json()["id"]
    script = c.post("/api/scripts", json={"name": "t", "data": {"stages": [{"turns": 1, "seeds": 2}]}}).json()
    exp = c.post("/api/experiments", json={"preset": ws["preset"], "script_id": script["id"], "scenario_ids": [sid], "connection_id": cid}).json()
    end = time.time() + 10
    while c.get(f"/api/experiments/{exp['id']}").json()["status"] == "running" and time.time() < end:
        time.sleep(0.05)
    run = c.get(f"/api/experiments/{exp['id']}").json()["runs"][0]
    c.post("/api/feedback", json={"target_type": "run", "target_id": run["id"], "text": "文風：4/10\n太多套話"})
    return exp["id"], cid


def test_builtin_loop_commits_and_reruns(env):
    holder = {}

    def patch(body):
        # 第一個 user 訊息裡帶了預設概覽與評語
        first = body["messages"][1]["content"]
        assert "太多套話" in first and "【目前的預設" in first
        tools = {t["function"]["name"]: t["function"] for t in body["tools"]}
        assert set(tools) == {"prompt", "scenario", "experiment"}
        assert {"patch", "commit"} <= set(tools["prompt"]["parameters"]["properties"]["action"]["enum"])
        assert tools["scenario"]["parameters"]["properties"]["action"]["enum"] == ["list", "read"]
        assert tools["experiment"]["parameters"]["properties"]["action"]["enum"] == ["feedback", "reply"]
        b = holder["block"]
        return completion("先修改主要區塊。", [
            ("prompt", {"action": "read", "path": f"blocks[{b['id']}]"}),
            ("prompt", {"action": "patch", "path": f"blocks[{b['id']}].content", "find": "沒有這段", "replace": "x"}),  # 錯誤 → 回報給模型
        ])

    def fix(body):
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        assert "找不到" in tool_msgs[-1]["content"]
        assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "prompt"
        b = holder["block"]
        return completion("", [
            ("scenario", {"action": "set", "scenario_id": 1, "path": "persona", "value": "x"}),  # 這一輪不能改情境
            ("prompt", {"action": "patch", "path": f"blocks[{b['id']}].content", "find": b["snippet"], "replace": b["snippet"] + "（避免套話）"}),
        ])

    script = Script([patch, fix, completion("", [("prompt", {"action": "commit", "message": "減少套話：主要區塊加入提醒"})])])
    fake = FakeLLM(default=script)
    with make(env, fake) as c:
        exp_id, cid = setup_experiment(c)
        b = content_block(c)
        full = tool(c, "prompt", "read", {"path": f"blocks[{b['id']}]"}).json()["result"]
        holder["block"] = {"id": b["id"], "snippet": full["content"][:12]}

        # 沒設定連線 → 拒絕
        assert c.post("/api/agent/rounds", json={"experiment_id": exp_id}).status_code == 422
        s = c.get("/api/agent/settings").json()
        c.put("/api/agent/settings", json=s | {"connection_id": cid})

        r = c.post("/api/agent/rounds", json={"experiment_id": exp_id, "auto_rerun": True}).json()
        assert r["branch"].startswith("llm/")  # main 受保護 → 先建分支
        done = wait_round(c, r["id"])
        assert done["status"] == "done", done
        assert len(done["steps"]) == 3 and done["tool_calls"] == 5
        assert done["steps"][0]["calls"][1]["ok"] is False
        assert "不能使用 scenario.set" in done["steps"][1]["calls"][0]["result"]
        assert done["cost"] == pytest.approx(0.003)

        log = c.get("/api/vcs/log").json()
        newest = log["commits"][0]
        assert newest["id"] == done["result_commit"] and newest["author"] == "llm:m"
        assert newest["meta"]["round_id"] == r["id"] and newest["parents"] == [done["base_commit"]]
        assert newest["message"].startswith("減少套話")
        assert "（避免套話）" in tool(c, "prompt", "read", {"path": f"blocks[{b['id']}].content"}).json()["result"]

        # 自動重跑：同一腳本快照，預設是新的 commit
        rerun = c.get(f"/api/experiments/{done['rerun_experiment_id']}").json()
        assert rerun["rerun_of"] == exp_id and rerun["commit_id"] == done["result_commit"]
        assert [x["id"] for x in c.get(f"/api/agent/rounds?experiment_id={exp_id}").json()] == [r["id"]]


def test_builtin_loop_guards(env):
    script = Script([
        completion("我覺得不用改。"),
        completion("", [("prompt", {"action": "add", "value": {"name": "提醒", "content": "不要替使用者說話。"}})]),
        completion("改好了，請你決定要不要提交。"),
    ])
    fake = FakeLLM(default=script)
    with make(env, fake) as c:
        exp_id, cid = setup_experiment(c)
        c.put("/api/agent/settings", json=c.get("/api/agent/settings").json() | {"connection_id": cid})

        # 工作區有未提交的變更 → 拒絕
        ws = c.get("/api/workspace").json()
        ws["preset"]["name"] = "改名"
        c.put("/api/workspace", json=ws)
        r = c.post("/api/agent/rounds", json={"experiment_id": exp_id})
        assert r.status_code == 422 and "未提交" in r.json()["detail"]
        c.post("/api/vcs/commit", json={"message": "改名"})

        # 模型沒改就結束
        done = wait_round(c, c.post("/api/agent/rounds", json={"experiment_id": exp_id}).json()["id"])
        assert done["status"] == "done" and "沒有做任何修改" in done["error"] and done["result_commit"] is None

        # 改了但沒提交 → uncommitted，可捨棄
        r2 = c.post("/api/agent/rounds", json={"instructions": "加一條提醒"}).json()
        done = wait_round(c, r2["id"])
        assert done["status"] == "uncommitted" and done["final_message"].startswith("改好了")
        assert c.get("/api/vcs/state").json()["dirty"] is True
        c.post(f"/api/agent/rounds/{r2['id']}/discard")
        assert c.get("/api/vcs/state").json()["dirty"] is False
