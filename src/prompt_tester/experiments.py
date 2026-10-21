"""金字塔實驗（design.md §3.1、§7.2）。

- 實驗 = 預設快照 × 解析後的腳本快照 × 情境快照 × 連線。
- 每條分支是一筆 run；晉級後的接續是子 run（parent_run_id），歷史 = 父 run 的情境歷史 + 父 run 的訊息，
  回合數沿用全域編號，因此對話樹就是 run 之間的父子關係。
- 每個階段在背景執行（併發上限由腳本決定），結束後一律等使用者確認晉級；晉級模式只決定預先勾選哪些分支。
- 重跑（rerun_of）沿用原實驗的腳本快照與情境；分支以 (階段, 情境, 溫度, seed, 父分支) 配對，供迴歸比較。
"""

import asyncio
import json
import re
import sqlite3
from typing import Any

import httpx
from pydantic import BaseModel

from . import feedback, providers, store, vcs
from .entities import Connection, ReasoningEffort, Scenario
from .preset import HistoryMessage, Preset
from .runner import CallResult, RunSpec, UserSimSettings, request_body, run_session, scripted_turns, stream_call
from .scripts import ScriptData, snapshot_hash

USER_SIM_KEY = "user_sim"
JUDGE_KEY = "judge"

DEFAULT_CRITERIA = """- 角色一致：言行符合角色卡設定，沒有出戲或拒答
- 格式：狀態欄、標籤等預設要求的結構完整正確
- 文風：生動、具體，沒有空泛套話與重複句型
- 劇情：自然回應使用者、有推進，不替使用者說話"""


class JudgeSettings(BaseModel):
    """評審 LLM（llm_assist 晉級）。connection_id 為空時沿用實驗的角色連線。"""

    connection_id: int | None = None
    criteria: str = DEFAULT_CRITERIA
    temperature: float = 0.2
    max_tokens: int = 4000
    reasoning_effort: ReasoningEffort | None = None
    # 每則回覆最多給評審看幾個字（保留格式，評審才看得到狀態欄等結構）
    max_chars: int = 6000


class ExperimentError(ValueError):
    pass


def load_user_sim(conn: sqlite3.Connection) -> UserSimSettings:
    return UserSimSettings.model_validate(store.get_setting(conn, USER_SIM_KEY) or {})


def load_judge(conn: sqlite3.Connection) -> JudgeSettings:
    return JudgeSettings.model_validate(store.get_setting(conn, JUDGE_KEY) or {})


def provider_for(conn: sqlite3.Connection, connection_id: int | None, what: str) -> tuple[Connection, providers.Provider]:
    c = store.get_connection(conn, connection_id) if connection_id is not None else None
    if c is None:
        raise ExperimentError(f"找不到{what}的連線 #{connection_id}")
    if not c.model:
        raise ExperimentError(f"連線「{c.name}」尚未選擇模型")
    p = providers.get_provider(c.provider)
    if p.is_openrouter and not p.api_key:
        raise ExperimentError("尚未設定 OpenRouter API key（設定頁或環境變數 OPENROUTER_API_KEY）")
    return c, p


# ---- 讀取 ----

def _exp_row(row) -> dict:
    d = dict(row)
    d["script_snapshot"] = json.loads(d["script_snapshot"])
    d["scenario_ids"] = json.loads(d["scenario_ids"])
    d["state"] = json.loads(d["state"])
    return d


def get_raw(conn: sqlite3.Connection, exp_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    return _exp_row(row) if row else None


def _set(conn: sqlite3.Connection, exp_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with conn:
        conn.execute(f"UPDATE experiments SET {cols} WHERE id = ?", (*vals, exp_id))


def list_experiments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id, e.created_at, e.name, e.status, e.current_stage, e.preset_name, e.commit_id, e.script_name,
                  e.rerun_of, json_array_length(json_extract(e.script_snapshot, '$.stages')) AS stage_count,
                  (SELECT COUNT(*) FROM runs r WHERE r.experiment_id = e.id) AS runs,
                  (SELECT COALESCE(SUM(r.cost), 0) FROM runs r WHERE r.experiment_id = e.id) AS run_cost,
                  json_extract(e.state, '$.judge_cost') AS judge_cost
           FROM experiments e ORDER BY e.id DESC"""
    ).fetchall()
    scores = _exp_scores(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["cost"] = (d.pop("run_cost") or 0) + (d.pop("judge_cost") or 0)
        d["score"] = scores.get(d["id"])
        out.append(d)
    return out


def _exp_scores(conn: sqlite3.Connection) -> dict[int, float]:
    by_run = feedback.overall_by_run(conn)
    acc: dict[int, list[float]] = {}
    for r in conn.execute("SELECT id, experiment_id FROM runs WHERE experiment_id IS NOT NULL"):
        if r["id"] in by_run:
            acc.setdefault(r["experiment_id"], []).append(by_run[r["id"]])
    return {k: round(sum(v) / len(v), 2) for k, v in acc.items()}


_EXCERPT_STRIP = re.compile(r"<(details|style|think|thinking|script)\b[^>]*>.*?</\1\s*>|<[^>]+>|```.*?```", re.S | re.I)


def _excerpt(text: str, n: int = 140) -> str:
    t = re.sub(r"\s+", " ", _EXCERPT_STRIP.sub(" ", text)).strip()
    return t[:n] + ("…" if len(t) > n else "")


def pair_keys(runs: list[dict]) -> dict[int, tuple]:
    """(階段, 情境, 溫度, seed, 父分支的 key)：同一腳本的兩次實驗中位置相同的分支有相同的 key。"""
    by_id = {r["id"]: r for r in runs}
    keys: dict[int, tuple] = {}

    def key(rid: int) -> tuple:
        if rid not in keys:
            r = by_id[rid]
            parent = key(r["parent_run_id"]) if r["parent_run_id"] in by_id else None
            keys[rid] = (r["stage"], r["scenario_id"], r["temperature"], r["seed"], parent)
        return keys[rid]

    for rid in by_id:
        key(rid)
    return keys


def detail(conn: sqlite3.Connection, exp_id: int) -> dict | None:
    exp = get_raw(conn, exp_id)
    if exp is None:
        return None
    runs = sorted(store.list_runs(conn, limit=10_000, experiment_id=exp_id), key=lambda r: r["id"])
    scores = feedback.overall_by_run(conn)
    last = {
        r["run_id"]: r["content"]
        for r in conn.execute(
            """SELECT m.run_id, m.content FROM run_messages m JOIN runs r ON r.id = m.run_id
               WHERE r.experiment_id = ? AND m.role = 'assistant' ORDER BY m.idx""",
            (exp_id,),
        )
    }
    for r in runs:
        r["score"] = scores.get(r["id"])
        r["excerpt"] = _excerpt(last.get(r["id"], ""))
        r["counterpart"] = None
    if exp["rerun_of"]:
        mine = pair_keys(runs)
        theirs = {k: rid for rid, k in pair_keys(store.list_runs(conn, limit=10_000, experiment_id=exp["rerun_of"])).items()}
        for r in runs:
            r["counterpart"] = theirs.get(mine[r["id"]])
        # 原實驗被晉級的分支，供「照原實驗晉級」預選
        exp["origin_promoted"] = [
            r["id"] for r in store.list_runs(conn, limit=10_000, experiment_id=exp["rerun_of"]) if r["promoted"]
        ]
    exp["runs"] = runs
    exp["cost"] = sum(r["cost"] or 0 for r in runs) + (exp["state"].get("judge_cost") or 0)
    exp["scenarios"] = {
        sid: s.get("name", "") for sid, s in exp["state"].get("scenarios", {}).items()
    }
    return exp


# ---- 建立與規劃 ----

def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    preset: Preset,
    script_id: int | None,
    script_name: str,
    script: ScriptData,
    scenario_ids: list[int],
    connection_id: int,
    rerun_of: int | None = None,
) -> int:
    if not scenario_ids:
        raise ExperimentError("請至少選一個情境")
    scenarios = {}
    for sid in scenario_ids:
        sc = store.get_scenario(conn, sid)
        if sc is None:
            raise ExperimentError(f"找不到情境 #{sid}")
        scenarios[str(sid)] = sc.model_dump(mode="json")
    provider_for(conn, connection_id, "角色")
    _check_user_sim(conn, script, [Scenario.model_validate(s) for s in scenarios.values()])
    preset_hash = store.snapshot_preset(conn, preset)
    with conn:
        exp_id = conn.execute(
            """INSERT INTO experiments (created_at, name, status, current_stage, preset_name, preset_hash, commit_id,
                                        script_id, script_name, script_snapshot, script_hash, scenario_ids,
                                        connection_id, rerun_of, state)
               VALUES (?, ?, 'running', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                store.now_iso(), name, preset.name, preset_hash, vcs.commit_for_tree(conn, vcs.tree_id_of(preset)),
                script_id, script_name, script.model_dump_json(), snapshot_hash(script), json.dumps(scenario_ids),
                connection_id, rerun_of, json.dumps({"scenarios": scenarios}, ensure_ascii=False),
            ),
        ).lastrowid
    return exp_id


def _check_user_sim(conn: sqlite3.Connection, script: ScriptData, scenarios: list[Scenario]) -> None:
    max_turns = max(s.turns for s in script.stages)
    needs = any(
        any(t not in scripted_turns(sc, script.user_source) for t in range(1, max_turns + 1)) for sc in scenarios
    )
    if needs:
        settings = load_user_sim(conn)
        if settings.connection_id is None:
            raise ExperimentError("這個腳本需要 LLM 扮演使用者，但尚未設定模擬使用者的連線（設定頁 → 模擬使用者）")
        provider_for(conn, settings.connection_id, "模擬使用者")


def _parent_history(run: dict) -> tuple[Scenario, int]:
    """接續父分支：情境快照的歷史接上父分支的訊息；回傳 (新情境快照, 已完成的回合數)。"""
    sc = Scenario.model_validate(run["scenario"])
    msgs = [m for m in run["messages"] if not m["error"]]
    sc = sc.model_copy(update={"history": sc.history + [HistoryMessage(role=m["role"], content=m["content"]) for m in msgs]})
    done = max((m["turn"] for m in msgs if m["role"] == "assistant"), default=0)
    return sc, done


def plan_stage(conn: sqlite3.Connection, exp: dict, stage_idx: int, parents: list[int] | None) -> list[int]:
    """建立這個階段的 run（狀態 queued），回傳 run id。parents 為 None 時從情境開場開始。"""
    script = ScriptData.model_validate(exp["script_snapshot"])
    stage = script.stages[stage_idx]
    preset = _preset(conn, exp["preset_hash"])
    connection, _ = provider_for(conn, exp["connection_id"], "角色")
    user_settings = load_user_sim(conn)
    user_conn = store.get_connection(conn, user_settings.connection_id) if user_settings.connection_id else None

    starts: list[tuple[Scenario, int | None, int]] = []  # (情境快照, 父 run, 已完成回合)
    if parents is None:
        for sid in exp["scenario_ids"]:
            starts.append((Scenario.model_validate(exp["state"]["scenarios"][str(sid)]), None, 0))
    else:
        for rid in parents:
            run = store.get_run(conn, rid)
            sc, done = _parent_history(run)
            if stage.turns <= done:
                raise ExperimentError(
                    f"第 {stage_idx + 1} 階段的累計回合數（{stage.turns}）必須大於分支 #{rid} 已完成的 {done} 回合"
                )
            starts.append((sc, rid, done))

    temps: list[float | None] = list(stage.temperatures) or [None]
    ids = []
    for sc, parent, done in starts:
        for temp in temps:
            for seed in range(1, stage.seeds + 1):
                ids.append(
                    store.create_run(
                        conn, mode="live", replay_of=None, turns=stage.turns - done, seed=seed, preset=preset,
                        preset_hash=exp["preset_hash"], commit_id=exp["commit_id"], scenario=sc,
                        connection=connection, user_connection=user_conn,
                        user_settings=user_settings.model_dump() | {"user_source": script.user_source, "turn_offset": done},
                        status="queued", experiment_id=exp["id"], stage=stage_idx, parent_run_id=parent,
                        temperature=temp,
                    )
                )
    return ids


def _preset(conn: sqlite3.Connection, preset_hash: str) -> Preset:
    row = conn.execute("SELECT data FROM preset_snapshots WHERE hash = ?", (preset_hash,)).fetchone()
    return Preset.model_validate_json(row["data"])


def _spec_for(conn: sqlite3.Connection, run_id: int, exp: dict) -> RunSpec:
    run = store.get_run(conn, run_id)
    preset = _preset(conn, run["preset_hash"])
    connection, provider = provider_for(conn, exp["connection_id"], "角色")
    us = dict(run["user_settings"] or {})
    source, offset = us.pop("user_source", "scenario_first"), us.pop("turn_offset", 0)
    settings = UserSimSettings.model_validate(us)
    user_conn = user_provider = None
    if settings.connection_id is not None:
        try:
            user_conn, user_provider = provider_for(conn, settings.connection_id, "模擬使用者")
        except ExperimentError:
            pass  # 需要時 run_session 會回報「沒有設定模擬使用者的連線」
    scenario = Scenario.model_validate(run["scenario"])
    root = Scenario.model_validate(exp["state"]["scenarios"][str(run["scenario_id"])])
    return RunSpec(
        preset=preset, scenario=scenario, connection=connection, provider=provider, turns=run["turns"],
        seed=run["seed"], mode="live", user_connection=user_conn, user_provider=user_provider,
        user_settings=settings, scripted_user_turns=scripted_turns(root, source), turn_offset=offset,
        temperature=run["temperature"],
    )


# ---- 執行 ----

class Manager:
    """背景執行各實驗的階段。一個實驗同時只有一個階段在跑。"""

    def __init__(self, conn: sqlite3.Connection, http: httpx.AsyncClient, lookup_stats: bool = True):
        self.conn = conn
        self.http = http
        self.lookup_stats = lookup_stats
        self.tasks: dict[int, asyncio.Task] = {}

    def running(self, exp_id: int) -> bool:
        t = self.tasks.get(exp_id)
        return t is not None and not t.done()

    def start_stage(self, exp_id: int, stage_idx: int, run_ids: list[int]) -> None:
        self.tasks[exp_id] = asyncio.create_task(self._execute(exp_id, stage_idx, run_ids))

    async def _execute(self, exp_id: int, stage_idx: int, run_ids: list[int]) -> None:
        conn = self.conn
        exp = get_raw(conn, exp_id)
        script = ScriptData.model_validate(exp["script_snapshot"])
        sem = asyncio.Semaphore(script.concurrency)

        async def one(run_id: int) -> None:
            async with sem:
                spec = _spec_for(conn, run_id, exp)
                with conn:
                    conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))
                async for _ in run_session(conn, self.http, spec, run_id, lookup_stats=self.lookup_stats):
                    pass

        try:
            results = await asyncio.gather(*(one(r) for r in run_ids), return_exceptions=True)
            for rid, res in zip(run_ids, results):
                if isinstance(res, Exception):
                    store.finish_run(conn, rid, status="error", error=str(res), cost=0, checks={"build_warnings": [], "issues": []})
        except asyncio.CancelledError:
            with conn:
                conn.execute(
                    "UPDATE runs SET status = 'cancelled', error = '實驗已中止' WHERE experiment_id = ? AND status IN ('queued', 'running')",
                    (exp_id,),
                )
            _set(conn, exp_id, status="cancelled")
            raise

        last = stage_idx == len(script.stages) - 1
        if not last and script.stages[stage_idx].promote.mode == "llm_assist":
            try:
                await self.judge(exp_id, stage_idx)
            except Exception as e:  # noqa: BLE001 — 評審失敗不影響實驗結果，記錄後由使用者手動挑選
                _record_judge(conn, exp_id, stage_idx, {"error": str(e)})
        _set(conn, exp_id, status="done" if last else "waiting")

    def cancel(self, exp_id: int) -> bool:
        t = self.tasks.get(exp_id)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    async def shutdown(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    # ---- 晉級 ----

    def advance(self, exp_id: int, run_ids: list[int]) -> dict:
        conn = self.conn
        exp = get_raw(conn, exp_id)
        if exp is None:
            raise KeyError(f"找不到實驗 #{exp_id}")
        if self.running(exp_id) or exp["status"] == "running":
            raise ExperimentError("這個階段還在執行中")
        script = ScriptData.model_validate(exp["script_snapshot"])
        cur = exp["current_stage"]
        if cur >= len(script.stages) - 1:
            raise ExperimentError("已經是最後一個階段")
        mode = script.stages[cur].promote.mode
        stage_runs = {r["id"]: r for r in store.list_runs(conn, limit=10_000, experiment_id=exp_id) if r["stage"] == cur}
        parents: list[int] | None
        if mode == "restart":
            parents = None
        else:
            if not run_ids:
                raise ExperimentError("請至少勾選一條要晉級的分支")
            bad = [rid for rid in run_ids if rid not in stage_runs or stage_runs[rid]["status"] != "done"]
            if bad:
                raise ExperimentError(f"只能晉級本階段已完成的分支（#{', #'.join(map(str, bad))} 不符合）")
            parents = sorted(set(run_ids))
        with conn:
            conn.execute("UPDATE runs SET promoted = 0 WHERE experiment_id = ? AND stage = ?", (exp_id, cur))
            if parents:
                conn.executemany("UPDATE runs SET promoted = 1 WHERE id = ?", [(r,) for r in parents])
        new_ids = plan_stage(conn, exp, cur + 1, parents)
        _set(conn, exp_id, current_stage=cur + 1, status="running", error=None)
        self.start_stage(exp_id, cur + 1, new_ids)
        return {"stage": cur + 1, "runs": len(new_ids)}

    # ---- 評審預選（llm_assist） ----

    async def judge(self, exp_id: int, stage_idx: int) -> dict:
        conn = self.conn
        exp = get_raw(conn, exp_id)
        script = ScriptData.model_validate(exp["script_snapshot"])
        k = script.stages[stage_idx].promote.k
        settings = load_judge(conn)
        connection, provider = provider_for(conn, settings.connection_id or exp["connection_id"], "評審")
        if settings.reasoning_effort is not None:
            connection = connection.model_copy(update={"reasoning_effort": settings.reasoning_effort})
        preset = _preset(conn, exp["preset_hash"])
        runs = [r for r in store.list_runs(conn, limit=10_000, experiment_id=exp_id) if r["stage"] == stage_idx and r["status"] == "done"]
        groups: dict[int, list[dict]] = {}
        for r in sorted(runs, key=lambda r: r["id"]):
            groups.setdefault(r["scenario_id"], []).append(r)

        picks: dict[str, dict] = {}
        cost, errors = 0.0, []
        for sid, group in groups.items():
            full = [store.get_run(conn, r["id"]) for r in group]
            notes = {r["id"]: [f["text"] for f in feedback.list_for(conn, run_id=r["id"])] for r in group}
            messages = judge_messages(settings, exp["state"]["scenarios"][str(sid)], full, notes, k)
            body = request_body(messages, preset.params, connection, None, provider,
                                temperature=settings.temperature, max_tokens=settings.max_tokens)
            res = CallResult()
            async for _ in stream_call(self.http, provider, body, res, lookup_stats=self.lookup_stats):
                pass
            cost += res.cost or 0
            if res.error:
                errors.append(res.error)
                continue
            try:
                ranking = parse_judge(res.output, [r["id"] for r in group])
            except ValueError as e:
                errors.append(f"{e}（情境 #{sid}）")
                continue
            picks.update({str(rid): v for rid, v in ranking.items()})

        result = {
            "picks": picks,
            "criteria": settings.criteria,
            "model": connection.model,
            "cost": cost,
            "error": "；".join(errors) or None,
            "at": store.now_iso(),
        }
        _record_judge(conn, exp_id, stage_idx, result)
        return result


def _record_judge(conn: sqlite3.Connection, exp_id: int, stage_idx: int, result: dict) -> None:
    exp = get_raw(conn, exp_id)
    state = exp["state"]
    state.setdefault("judge", {})[str(stage_idx)] = result
    state["judge_cost"] = (state.get("judge_cost") or 0) + (result.get("cost") or 0)
    _set(conn, exp_id, state=state)


def judge_messages(settings: JudgeSettings, scenario: dict, runs: list[dict], notes: dict[int, list[str]], k: int) -> list[dict]:
    char, user = scenario.get("char", "角色"), scenario.get("user", "使用者")
    system = (
        "你是角色扮演提示詞測試的評審。同一個情境、同一份提示詞跑出了多條候選對話，"
        f"請依使用者的挑選標準排序，並挑出最值得晉級（繼續跑更多回合）的至多 {k} 條。"
        "只根據對話內容判斷，不要因為長度本身加分。只輸出 JSON。"
    )
    parts = [f"【挑選標準】\n{settings.criteria.strip() or '（未指定，依整體品質）'}", f"【最多挑選】{k} 條"]
    for i, run in enumerate(runs, 1):
        lines = []
        for m in run["messages"]:
            if m["error"]:
                continue
            speaker = user if m["role"] == "user" else char
            text = m["content"]
            if len(text) > settings.max_chars:
                text = text[: settings.max_chars] + "…（以下省略）"
            lines.append(f"[{speaker}]\n{text}")
        block = f"## 候選 {i}\n" + "\n\n".join(lines)
        if notes.get(run["id"]):
            block += "\n\n（使用者評語）\n" + "\n---\n".join(notes[run["id"]])
        parts.append(block)
    parts.append(
        '輸出格式：{"ranking": [{"candidate": 1, "pick": true, "reason": "一句話理由"}, ...]}。'
        "每個候選都要列出，依好到壞排序；pick 為 true 的最多 "
        f"{k} 個。理由使用繁體中文。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def parse_judge(text: str, run_ids: list[int]) -> dict[int, dict]:
    """候選編號 → run id；回傳 {run_id: {pick, rank, reason}}。"""
    t = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("評審沒有輸出 JSON")
    try:
        data = json.loads(t[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"評審輸出的 JSON 無法解析：{e.msg}") from e
    out: dict[int, dict] = {}
    for rank, item in enumerate(data.get("ranking") or [], 1):
        try:
            n = int(item["candidate"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= n <= len(run_ids) and run_ids[n - 1] not in out:
            out[run_ids[n - 1]] = {"pick": bool(item.get("pick")), "rank": rank, "reason": str(item.get("reason", ""))}
    if not out:
        raise ValueError("評審的排序是空的")
    return out


def delete(conn: sqlite3.Connection, exp_id: int) -> None:
    ids = [r["id"] for r in conn.execute("SELECT id FROM runs WHERE experiment_id = ?", (exp_id,))]
    for rid in ids:
        store.delete_run(conn, rid)
    with conn:
        conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
