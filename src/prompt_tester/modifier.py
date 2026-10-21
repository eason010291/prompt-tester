"""① 內建修改迴圈（design.md §7.2–7.3）：任一 OpenAI 相容模型以 tool calling 依評語修改工作區並提交。

- 一次只跑一輪（工作區只有一份）。開始前工作區必須沒有未提交的變更，提交的 diff 才會只有這輪的修改。
- 在受保護分支（main）上時，開始就建立 `llm/<時間>` 分支並切換過去。
- 非串流呼叫：tool_calls 與 reasoning_details 原樣送回（Gemini / DeepSeek 推理 + 工具需要）。
- 上限：LLM 呼叫次數、工具呼叫次數、花費。每次呼叫後寫入紀錄，前端輪詢顯示進度。
- 提交後可自動用新版本重跑來源實驗（同一腳本快照與情境）。
"""

import asyncio
import json
import sqlite3

import httpx

from . import agent_tools as at
from . import experiments, llm, store, vcs, workspace
from .preset import Params
from .runner import estimate_cost, request_body


class ModifierError(ValueError):
    pass


def _row(row) -> dict:
    d = dict(row)
    d["steps"] = json.loads(d["steps"])
    d["connection"] = json.loads(d["connection"])
    d["auto_rerun"] = bool(d["auto_rerun"])
    return d


def get_round(conn: sqlite3.Connection, rid: int) -> dict | None:
    row = conn.execute("SELECT * FROM agent_rounds WHERE id = ?", (rid,)).fetchone()
    return _row(row) if row else None


def list_rounds(conn: sqlite3.Connection, experiment_id: int | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM agent_rounds"
    args: tuple = ()
    if experiment_id is not None:
        sql += " WHERE experiment_id = ?"
        args = (experiment_id,)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d["step_count"] = len(d.pop("steps"))
        out.append(d)
    return out


def _update(conn: sqlite3.Connection, rid: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in fields.values()]
    with conn:
        conn.execute(f"UPDATE agent_rounds SET {cols} WHERE id = ?", (*vals, rid))


def initial_message(ctx: at.Ctx, instructions: str, experiment_id: int | None) -> str:
    parts = [f"【使用者的要求】\n{instructions.strip() or '依下方評語改善預設。'}"]
    parts.append("【目前的預設（list_blocks）】\n" + at.to_text(at.call(ctx, "list_blocks", {}), 12000))
    if experiment_id is not None:
        fb = at.call(ctx, "get_feedback", {"experiment_id": experiment_id})
        parts.append(f"【實驗 #{experiment_id} 的評語（get_feedback）】\n" + at.to_text(fb, 40000))
    parts.append("請開始：必要時讀取區塊全文，修改後呼叫 commit。")
    return "\n\n".join(parts)


class Manager:
    def __init__(self, conn: sqlite3.Connection, http: httpx.AsyncClient, experiments_manager: experiments.Manager):
        self.conn = conn
        self.http = http
        self.experiments = experiments_manager
        self.tasks: dict[int, asyncio.Task] = {}

    def running(self) -> int | None:
        for rid, t in self.tasks.items():
            if not t.done():
                return rid
        return None

    def start(self, *, instructions: str, experiment_id: int | None, auto_rerun: bool,
              connection_id: int | None = None) -> int:
        conn = self.conn
        if self.running() is not None:
            raise ModifierError("已經有一輪修改在進行中")
        settings = at.load_settings(conn)
        cid = connection_id or settings.connection_id
        if cid is None:
            raise ModifierError("尚未設定修改者的連線（設定頁 → 修改者 LLM）")
        connection, _provider = experiments.provider_for(conn, cid, "修改者")
        if experiment_id is not None and experiments.get_raw(conn, experiment_id) is None:
            raise ModifierError(f"找不到實驗 #{experiment_id}")
        if experiment_id is not None and not instructions.strip():
            has_fb = conn.execute(
                "SELECT 1 FROM feedback f JOIN runs r ON r.id = f.run_id WHERE r.experiment_id = ? LIMIT 1", (experiment_id,)
            ).fetchone()
            if not has_fb:
                raise ModifierError("這個實驗還沒有評語；先寫評語，或在要求欄說明要改什麼")
        if experiment_id is None and not instructions.strip():
            raise ModifierError("請寫下要修改的方向")
        _, base = workspace.head(conn)
        if base is None:
            raise ModifierError("還沒有任何提交；請先提交一次作為修改的基準")
        if workspace.is_dirty(conn):
            raise ModifierError("工作區有未提交的變更；請先提交或捨棄，修改結果才不會混在一起")
        branch = workspace.ensure_unprotected_branch(conn, "llm")
        with conn:
            rid = conn.execute(
                """INSERT INTO agent_rounds (created_at, status, author, connection, instructions, experiment_id,
                                             auto_rerun, base_commit, branch)
                   VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)""",
                (store.now_iso(), f"llm:{connection.model}", connection.model_dump_json(), instructions,
                 experiment_id, int(auto_rerun), base, branch),
            ).lastrowid
        self.tasks[rid] = asyncio.create_task(self._run(rid, cid, settings))
        return rid

    def cancel(self, rid: int) -> bool:
        t = self.tasks.get(rid)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    async def shutdown(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _run(self, rid: int, connection_id: int, settings: at.AgentSettings) -> None:
        conn = self.conn
        rnd = get_round(conn, rid)
        connection, provider = experiments.provider_for(conn, connection_id, "修改者")
        ctx = at.Ctx(conn=conn, author=rnd["author"], settings=settings, manager=self.experiments,
                     meta={"round_id": rid, "experiment_id": rnd["experiment_id"], "model": connection.model},
                     branch_prefix="llm")
        names = at.builtin_names(settings)
        tools = at.openai_tools(names)
        steps: list[dict] = []
        cost, n_calls = 0.0, 0
        status, error, final = "done", None, None
        try:
            messages: list[dict] = [
                {"role": "system", "content": settings.system_prompt},
                {"role": "user", "content": initial_message(ctx, rnd["instructions"], rnd["experiment_id"])},
            ]
            for step in range(1, settings.max_steps + 1):
                body = request_body(messages, Params(), connection, None, provider,
                                    temperature=settings.temperature, max_tokens=settings.max_tokens)
                body["tools"] = tools
                body["tool_choice"] = "auto"
                data = await llm.chat_complete(self.http, provider, body)
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                step_cost = usage.get("cost")
                if step_cost is None:
                    step_cost = estimate_cost(provider, connection.model, usage) or 0
                cost += step_cost
                calls = msg.get("tool_calls") or []
                entry = {
                    "step": step,
                    "text": msg.get("content") or "",
                    "reasoning": (msg.get("reasoning") or msg.get("reasoning_content") or "")[:4000],
                    "calls": [],
                    "cost": step_cost,
                    "finish_reason": choice.get("finish_reason"),
                    "tokens": [usage.get("prompt_tokens"), usage.get("completion_tokens")],
                }
                assistant = {"role": "assistant", "content": msg.get("content") or ""}
                if calls:
                    assistant["tool_calls"] = calls
                if msg.get("reasoning_details"):
                    assistant["reasoning_details"] = msg["reasoning_details"]
                elif msg.get("reasoning"):
                    assistant["reasoning"] = msg["reasoning"]
                messages.append(assistant)

                for c in calls:
                    fn = c.get("function") or {}
                    name, raw = fn.get("name", ""), fn.get("arguments") or "{}"
                    n_calls += 1
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        args = raw
                    if n_calls > settings.max_tool_calls:
                        ok, result = False, f"已達工具呼叫上限（{settings.max_tool_calls}），請立刻 commit 或結束"
                    elif name not in names:
                        ok, result = False, f"這個迴圈不能使用工具 {name}"
                    else:
                        try:
                            ok, result = True, at.call(ctx, name, args)
                        except at.ToolError as e:
                            ok, result = False, str(e)
                    text = at.to_text(result if ok else {"error": result})
                    entry["calls"].append({"name": name, "args": args, "ok": ok, "result": at.to_text(result, 1500)})
                    messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": name, "content": text})
                    if ctx.committed:
                        break

                steps.append(entry)
                _update(conn, rid, steps=steps, cost=cost, tool_calls=n_calls)
                if ctx.committed:
                    final = entry["text"] or None
                    break
                if not calls:
                    final = entry["text"]
                    status = "uncommitted" if ctx.changed else "done"
                    if not ctx.changed:
                        error = "模型沒有修改任何區塊就結束了"
                    break
                if cost > settings.budget_usd:
                    status, error = "error", f"花費 ${cost:.4f} 超過上限 ${settings.budget_usd}"
                    break
                if n_calls > settings.max_tool_calls:
                    status, error = "error", f"超過工具呼叫上限（{settings.max_tool_calls}）"
                    break
            else:
                status, error = "error", f"達到 LLM 呼叫上限（{settings.max_steps}）仍未提交"
            if not ctx.committed and status == "done" and ctx.changed:
                status = "uncommitted"
        except asyncio.CancelledError:
            _update(conn, rid, status="cancelled", error="使用者中止", steps=steps, cost=cost, tool_calls=n_calls)
            raise
        except (llm.LLMError, at.ToolError, vcs.VcsError, experiments.ExperimentError) as e:
            status, error = "error", str(e)

        fields: dict = {"status": status, "error": error, "final_message": final, "steps": steps, "cost": cost,
                        "tool_calls": n_calls}
        if ctx.committed:
            fields |= {"status": "done", "result_commit": ctx.committed["commit"], "branch": ctx.committed["branch"]}
            if rnd["auto_rerun"] and rnd["experiment_id"] is not None:
                try:
                    fields["rerun_experiment_id"] = experiments.create_rerun(
                        conn, self.experiments, rnd["experiment_id"], workspace.working(conn)
                    )
                except (experiments.ExperimentError, ValueError) as e:
                    fields["error"] = f"已提交，但重跑失敗：{e}"
        _update(conn, rid, **fields)


def discard(conn: sqlite3.Connection, rid: int) -> None:
    """捨棄一輪未提交的修改：工作區還原成這輪開始時的基準版本。"""
    rnd = get_round(conn, rid)
    if rnd is None:
        raise ModifierError(f"找不到修改紀錄 #{rid}")
    if rnd["result_commit"]:
        raise ModifierError("這輪已經提交；要放棄請在版本樹切回原本的版本")
    branch, base = workspace.head(conn)
    if base != rnd["base_commit"]:
        raise ModifierError("工作區已經換到別的版本，無法自動還原")
    preset = vcs.read_tree(conn, vcs.get_commit(conn, base).tree_id, collapsed_from=workspace.working(conn))
    workspace.save(conn, preset, branch, base)
    _update(conn, rid, status="cancelled" if rnd["status"] == "running" else rnd["status"], error="已捨棄修改")
