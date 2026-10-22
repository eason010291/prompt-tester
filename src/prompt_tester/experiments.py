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
from . import metrics as metrics_mod
from . import scenario_revisions as revisions
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


def pair_keys(runs: list[dict], origin_keys: dict[int, tuple] | None = None) -> dict[int, tuple]:
    """(階段, 情境, 溫度, seed, 父分支的 key)：同一腳本的兩次實驗中位置相同的分支有相同的 key。
    部分迴歸的分支直接沿用原分支（origin_run_id）的 key。"""
    by_id = {r["id"]: r for r in runs}
    keys: dict[int, tuple] = {}

    def key(rid: int) -> tuple:
        if rid not in keys:
            r = by_id[rid]
            if r.get("origin_run_id") and origin_keys and r["origin_run_id"] in origin_keys:
                keys[rid] = origin_keys[r["origin_run_id"]]
                return keys[rid]
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
    defs = metrics_mod.load(conn)
    replies: dict[int, list[dict]] = {}
    for m in conn.execute(
        """SELECT m.id, m.run_id, m.role, m.content, m.error FROM run_messages m JOIN runs r ON r.id = m.run_id
           WHERE r.experiment_id = ? AND m.role = 'assistant' ORDER BY m.idx""",
        (exp_id,),
    ):
        replies.setdefault(m["run_id"], []).append(dict(m))
    for r in runs:
        mine = replies.get(r["id"], [])
        r["score"] = scores.get(r["id"])
        r["excerpt"] = _excerpt(mine[-1]["content"] if mine else "")
        r["metrics"] = metrics_mod.run_metrics(mine, defs)["totals"]
        r["counterpart"] = None
    if exp["rerun_of"]:
        origin_keys = pair_keys(store.list_runs(conn, limit=10_000, experiment_id=exp["rerun_of"]))
        mine = pair_keys(runs, origin_keys)
        theirs = {k: rid for rid, k in origin_keys.items()}
        for r in runs:
            r["counterpart"] = theirs.get(mine[r["id"]])
        # 原實驗被晉級的分支，供「照原實驗晉級」預選
        exp["origin_promoted"] = [
            r["id"] for r in store.list_runs(conn, limit=10_000, experiment_id=exp["rerun_of"]) if r["promoted"]
        ]
    exp["runs"] = runs
    exp["metric_defs"] = metrics_mod.describe(defs)
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
    revs: dict[str, int] = {}
    for sid in scenario_ids:
        sc = store.get_scenario(conn, sid)
        if sc is None:
            raise ExperimentError(f"找不到情境 #{sid}")
        scenarios[str(sid)] = sc.model_dump(mode="json")
        revs[str(sid)] = revisions.ensure(conn, sc, message="建立實驗時自動存成版本")["id"]
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
                connection_id, rerun_of, json.dumps({"scenarios": scenarios, "scenario_revisions": revs}, ensure_ascii=False),
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


def plan_stage(conn: sqlite3.Connection, exp: dict, stage_idx: int, parents: list[int] | None,
               manual_turns: dict[int, str] | None = None) -> list[int]:
    """建立這個階段的 run（狀態 queued），回傳 run id。parents 為 None 時從情境開場開始。
    manual_turns：使用者接管輸入時預先寫好的使用者發言 {全域回合: 內容}，套用到本階段所有分支。"""
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
                        user_settings=user_settings.model_dump() | {
                            "user_source": script.user_source, "turn_offset": done,
                            "manual_turns": {str(t): v for t, v in (manual_turns or {}).items() if v.strip()},
                        },
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
    manual = {int(t): v for t, v in (us.pop("manual_turns", None) or {}).items()}
    keep = {int(t): (v[0], v[1]) for t, v in (us.pop("keep_turns", None) or {}).items()}
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
        temperature=run["temperature"], manual_user_turns=manual, keep_user_turns=keep,
    )


# ---- 執行 ----

class Manager:
    """背景執行各實驗的分支。

    - 一個實驗可以同時有多個工作（階段執行、重試），共用腳本的併發上限；全部結束後實驗才回到「等待晉級 / 完成」。
    - 每條分支各自是一個 task，可以單獨中止；排隊中的分支中止後就不會開始。
    """

    def __init__(self, conn: sqlite3.Connection, http: httpx.AsyncClient, lookup_stats: bool = True):
        self.conn = conn
        self.http = http
        self.lookup_stats = lookup_stats
        self.tasks: dict[int, set[asyncio.Task]] = {}
        self.sems: dict[int, asyncio.Semaphore] = {}
        self.run_tasks: dict[int, asyncio.Task] = {}
        self.cancelled_runs: set[int] = set()

    def running(self, exp_id: int) -> bool:
        return any(not t.done() for t in self.tasks.get(exp_id, ()))

    def _spawn(self, exp_id: int, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.setdefault(exp_id, set()).add(task)
        task.add_done_callback(lambda t: self.tasks.get(exp_id, set()).discard(t))

    def _sem(self, exp: dict) -> asyncio.Semaphore:
        if exp["id"] not in self.sems:
            self.sems[exp["id"]] = asyncio.Semaphore(ScriptData.model_validate(exp["script_snapshot"]).concurrency)
        return self.sems[exp["id"]]

    def _others_running(self, exp_id: int) -> bool:
        me = asyncio.current_task()
        return any(t is not me and not t.done() for t in self.tasks.get(exp_id, ()))

    def _settle(self, exp_id: int) -> None:
        """沒有其他工作在跑時，實驗回到等待晉級（或最後一階段的完成）。"""
        if self._others_running(exp_id):
            return
        exp = get_raw(self.conn, exp_id)
        last = exp["current_stage"] >= len(exp["script_snapshot"]["stages"]) - 1
        _set(self.conn, exp_id, status="done" if last else "waiting")

    async def _run_branch(self, exp: dict, run_id: int, spec_fn) -> None:
        """跑一條分支。spec_fn() 在取得併發名額後才建立 spec（讀取最新設定）。"""
        conn = self.conn
        async with self._sem(exp):
            if run_id in self.cancelled_runs:
                self.cancelled_runs.discard(run_id)
                return
            spec = spec_fn()
            with conn:
                conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))

            async def consume() -> None:
                async for _ in run_session(conn, self.http, spec, run_id, lookup_stats=self.lookup_stats):
                    pass

            task = asyncio.create_task(consume())
            self.run_tasks[run_id] = task
            try:
                await task
            except asyncio.CancelledError:
                outer = asyncio.current_task()
                if outer is not None and outer.cancelling():
                    raise  # 整個實驗中止或伺服器關閉
                # 只中止這條分支：run_session 已標記；保險起見再確認一次
                if (store.get_run(conn, run_id) or {}).get("status") == "running":
                    store.finish_run(conn, run_id, status="cancelled", error="已中止這條分支", cost=0,
                                     checks={"build_warnings": [], "issues": []})
            finally:
                self.run_tasks.pop(run_id, None)
                self.cancelled_runs.discard(run_id)

    async def _run_all(self, exp: dict, jobs: list[tuple[int, Any]]) -> None:
        conn = self.conn
        results = await asyncio.gather(*(self._run_branch(exp, rid, fn) for rid, fn in jobs), return_exceptions=True)
        for (rid, _), res in zip(jobs, results):
            if isinstance(res, Exception):
                store.finish_run(conn, rid, status="error", error=str(res), cost=0, checks={"build_warnings": [], "issues": []})

    def _on_cancel(self, exp_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status = 'cancelled', error = '實驗已中止' WHERE experiment_id = ? AND status IN ('queued', 'running')",
                (exp_id,),
            )
        if not self._others_running(exp_id):
            _set(self.conn, exp_id, status="cancelled")

    def start_stage(self, exp_id: int, stage_idx: int, run_ids: list[int]) -> None:
        self._spawn(exp_id, self._execute(exp_id, stage_idx, run_ids))

    async def _execute(self, exp_id: int, stage_idx: int, run_ids: list[int]) -> None:
        conn = self.conn
        exp = get_raw(conn, exp_id)
        script = ScriptData.model_validate(exp["script_snapshot"])
        try:
            await self._run_all(exp, [(rid, lambda rid=rid: _spec_for(conn, rid, exp)) for rid in run_ids])
        except asyncio.CancelledError:
            self._on_cancel(exp_id)
            raise
        last = stage_idx == len(script.stages) - 1
        if not last and script.stages[stage_idx].promote.mode == "llm_assist":
            try:
                await self.judge(exp_id, stage_idx)
            except Exception as e:  # noqa: BLE001 — 評審失敗不影響實驗結果，記錄後由使用者手動挑選
                _record_judge(conn, exp_id, stage_idx, {"error": str(e)})
        self._settle(exp_id)

    def cancel_run(self, exp_id: int, run_id: int) -> str:
        """中止單一分支：執行中的立刻中斷，排隊中的不再開始。"""
        run = store.get_run(self.conn, run_id)
        if run is None or run["experiment_id"] != exp_id:
            raise KeyError(f"找不到分支 #{run_id}")
        task = self.run_tasks.get(run_id)
        if task is not None and not task.done():
            self.cancelled_runs.add(run_id)
            task.cancel()
            return "cancelled"
        if run["status"] == "queued":
            self.cancelled_runs.add(run_id)
            store.finish_run(self.conn, run_id, status="cancelled", error="已中止這條分支", cost=run["cost"] or 0,
                             checks=run["checks"] or {"build_warnings": [], "issues": []})
            return "cancelled"
        if run["status"] == "running":
            # 沒有對應的工作（例如伺服器重啟前留下的狀態）：直接標記
            store.finish_run(self.conn, run_id, status="cancelled", error="已中止這條分支", cost=run["cost"] or 0,
                             checks=run["checks"] or {"build_warnings": [], "issues": []})
            return "cancelled"
        raise ExperimentError("這條分支沒有在執行")

    # ---- 重試 ----

    def descendants(self, run_id: int) -> list[int]:
        """接在這條分支後面的所有分支（下一階段、再下一階段…），由近到遠。"""
        out, frontier = [], [run_id]
        while frontier:
            kids = [r["id"] for f in frontier
                    for r in self.conn.execute("SELECT id FROM runs WHERE parent_run_id = ? ORDER BY id", (f,))]
            out += kids
            frontier = kids
        return out

    def retry(
        self, exp_id: int, targets: list[dict], *, user_text: str | None = None, later_users: str = "regenerate",
        tags: list[str] | None = None, note: str = "", scope: str | None = None,
    ) -> dict:
        """重試（同一個版本重新抽樣）。targets 每項：{run_id, kind, turn, role}

        - kind=message：刪除（封存）該訊息與之後的訊息，重新產生並續跑到原本的回合數
        - kind=branch：整條分支（本段）重新產生
        - kind=resume：從中斷處接著跑（最後一則出錯就重試它）
        接在後面的下一階段分支會連帶封存，等上層跑完後以新的對話重建並重跑。
        user_text：重試使用者發言時改用這段手動輸入。later_users：keep 沿用之後的使用者發言 / regenerate 重新產生。
        tags / note：重試原因（使用者勾選的標籤與「其他」說明），記入 retry_log。
        """
        conn = self.conn
        exp = get_raw(conn, exp_id)
        if exp is None:
            raise KeyError(f"找不到實驗 #{exp_id}")
        if later_users not in ("keep", "regenerate"):
            raise ExperimentError("later_users 必須是 keep 或 regenerate")
        if user_text is not None and not user_text.strip():
            raise ExperimentError("手動輸入的使用者發言不能是空的")

        busy = lambda rid: rid in self.run_tasks or (store.get_run(conn, rid) or {}).get("status") in ("queued", "running")  # noqa: E731
        root_ids = {t["run_id"] for t in targets}
        plans: list[tuple[int, RunSpec]] = []
        cuts: list[tuple[int, int, str]] = []  # (run, 從哪個 idx 起封存, 說明)
        logs: list[dict] = []
        skipped: list[dict] = []
        cascade: dict[int, list[int]] = {}
        child_keep: dict[int, dict[int, tuple[str, str]]] = {}
        seen: set[int] = set()

        for t in targets:
            rid, kind, turn, role = t["run_id"], t.get("kind", "message"), t.get("turn"), t.get("role")
            run = store.get_run(conn, rid)
            if run is None or run["experiment_id"] != exp_id:
                skipped.append({"run_id": rid, "reason": "不屬於這個實驗"})
                continue
            # 被其他目標連帶處理的分支不重複排入
            if any(rid in self.descendants(other) for other in root_ids if other != rid):
                skipped.append({"run_id": rid, "reason": "上層分支也在這次重試中，會連帶重跑"})
                continue
            if busy(rid):
                skipped.append({"run_id": rid, "reason": "還在執行中（可以先中止這條）"})
                continue
            desc = self.descendants(rid)
            if any(busy(d) for d in desc):
                skipped.append({"run_id": rid, "reason": "接在後面的分支還在執行中"})
                continue
            msgs = run["messages"]
            spec = _spec_for(conn, rid, exp)
            end_turn = spec.turn_offset + spec.turns
            retries = conn.execute("SELECT COUNT(DISTINCT archived_at) FROM run_message_archive WHERE run_id = ?",
                                   (rid,)).fetchone()[0]
            log_kind = "manual"
            if kind == "resume":
                log_kind = "error"
                last = msgs[-1] if msgs else None
                if last is not None and last["error"]:
                    kind, turn, role = "message", last["turn"], last["role"]
                else:
                    if last is None:
                        spec.start_turn, cut_from, what = spec.turn_offset + 1, 1, "從頭續跑"
                    elif last["role"] == "user":
                        spec.start_turn, spec.skip_user_at_start = last["turn"], True
                        cut_from, what = last["idx"] + 1, f"第 {last['turn']} 回合續跑"
                    elif last["turn"] >= end_turn:
                        skipped.append({"run_id": rid, "reason": "已經跑完，沒有可續跑的回合"})
                        continue
                    else:
                        spec.start_turn, cut_from, what = last["turn"] + 1, last["idx"] + 1, f"第 {last['turn'] + 1} 回合續跑"
                    spec.resume_messages = msgs
                    spec.seed_offset = 1000 * retries
            if kind == "branch":
                spec.start_turn, cut_from, what = spec.turn_offset + 1, 1, "整條分支重新產生"
                if later_users == "keep":
                    spec.keep_user_turns = {m["turn"]: (m["content"], m["source"]) for m in msgs if m["role"] == "user" and not m["error"]}
                spec.seed_offset = 1000 * (retries + 1)
            elif kind == "message":
                target = next((m for m in msgs if m["turn"] == turn and m["role"] == role), None)
                if target is None:
                    skipped.append({"run_id": rid, "reason": f"沒有第 {turn} 回合的{'使用者發言' if role == 'user' else '角色回覆'}"})
                    continue
                spec.resume_messages = [m for m in msgs if m["idx"] < target["idx"]]
                spec.start_turn = turn
                spec.skip_user_at_start = role == "assistant"
                if role == "user":
                    if user_text is not None:
                        spec.manual_user_turns = {**spec.manual_user_turns, turn: user_text}
                    else:
                        spec.force_llm_user_turn = turn
                if later_users == "keep":
                    spec.keep_user_turns = {m["turn"]: (m["content"], m["source"]) for m in msgs
                                            if m["role"] == "user" and m["turn"] > turn and not m["error"]}
                spec.seed_offset = 1000 * (retries + 1)
                cut_from = target["idx"]
                what = ("手動改寫使用者發言" if role == "user" and user_text is not None
                        else "重試使用者發言" if role == "user" else "重試角色回覆")
                what = f"第 {turn} 回合{what}"
            elif kind != "resume":
                skipped.append({"run_id": rid, "reason": f"未知的重試方式：{kind}"})
                continue

            plans.append((rid, spec))
            cuts.append((rid, cut_from, what))
            logs.append({"run_id": rid, "scope": scope or ("resume" if log_kind == "error" else kind), "kind": log_kind,
                         "turn": turn if kind == "message" else None, "role": role if kind == "message" else None,
                         "model": run["connection"].get("model")})
            # 連帶：下一階段起接在後面的分支
            for d in desc:
                if d in seen:
                    continue
                seen.add(d)
                dr = store.get_run(conn, d)
                cascade.setdefault(dr["parent_run_id"], []).append(d)
                if later_users == "keep":
                    child_keep[d] = {m["turn"]: (m["content"], m["source"]) for m in dr["messages"]
                                     if m["role"] == "user" and not m["error"]}
                cuts.append((d, 1, "上層分支重試，連帶重跑"))
                logs.append({"run_id": d, "scope": "cascade", "kind": "cascade", "turn": None, "role": None,
                             "model": dr["connection"].get("model")})

        ts = store.now_iso()
        for rid, from_idx, reason in cuts:
            store.truncate_run(conn, rid, from_idx, reason)
            with conn:
                conn.execute("UPDATE runs SET status = 'queued', error = NULL WHERE id = ?", (rid,))
        with conn:
            conn.executemany(
                """INSERT INTO retry_log (created_at, experiment_id, run_id, scope, kind, turn, role, model, tags, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(ts, exp_id, x["run_id"], x["scope"], x["kind"], x["turn"], x["role"], x["model"],
                  json.dumps(tags or [], ensure_ascii=False) if x["kind"] == "manual" else "[]",
                  note if x["kind"] == "manual" else "") for x in logs],
            )
        if plans:
            _set(conn, exp_id, status="running")
            self._spawn(exp_id, self._run_specs(exp_id, plans, cascade, child_keep))
        return {"started": [rid for rid, _ in plans], "cascade": sorted(seen), "skipped": skipped}

    def _child_spec(self, exp: dict, child_id: int, keep: dict[int, tuple[str, str]] | None) -> RunSpec:
        """上層分支重跑完後：以新的對話重建子分支的情境快照與回合範圍，整段重新產生。"""
        conn = self.conn
        child = store.get_run(conn, child_id)
        parent = store.get_run(conn, child["parent_run_id"])
        sc, done = _parent_history(parent)
        end = (child["user_settings"] or {}).get("turn_offset", 0) + child["turns"]
        us = dict(child["user_settings"] or {})
        us["turn_offset"] = done
        store.update_run(conn, child_id, scenario=sc.model_dump(mode="json"), turns=max(1, end - done), user_settings=us)
        spec = _spec_for(conn, child_id, exp)
        retries = conn.execute("SELECT COUNT(DISTINCT archived_at) FROM run_message_archive WHERE run_id = ?",
                               (child_id,)).fetchone()[0]
        spec.seed_offset = 1000 * retries
        if keep:
            spec.keep_user_turns = {t: v for t, v in keep.items() if t > done}
        return spec

    async def _run_tree(self, exp: dict, run_id: int, spec_fn, cascade: dict[int, list[int]],
                        child_keep: dict[int, dict]) -> None:
        conn = self.conn
        try:
            await self._run_branch(exp, run_id, spec_fn)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 單一分支失敗不影響其他分支
            store.finish_run(conn, run_id, status="error", error=str(e), cost=0, checks={"build_warnings": [], "issues": []})
        kids = cascade.get(run_id, [])
        if not kids:
            return
        if (store.get_run(conn, run_id) or {}).get("status") != "done":
            for d in [k for kid in kids for k in [kid, *self.descendants(kid)]]:
                store.finish_run(conn, d, status="cancelled", error="上層分支沒有跑完，未重跑", cost=0,
                                 checks={"build_warnings": [], "issues": []})
            return
        await asyncio.gather(*(
            self._run_tree(exp, k, lambda k=k: self._child_spec(exp, k, child_keep.get(k)), cascade, child_keep)
            for k in kids
        ))

    async def _run_specs(self, exp_id: int, plans: list[tuple[int, RunSpec]], cascade: dict[int, list[int]] | None = None,
                         child_keep: dict[int, dict] | None = None) -> None:
        exp = get_raw(self.conn, exp_id)
        try:
            await asyncio.gather(*(
                self._run_tree(exp, rid, lambda spec=spec: spec, cascade or {}, child_keep or {}) for rid, spec in plans
            ))
        except asyncio.CancelledError:
            self._on_cancel(exp_id)
            raise
        self._settle(exp_id)

    def cancel(self, exp_id: int) -> bool:
        tasks = [t for t in self.tasks.get(exp_id, ()) if not t.done()]
        for t in tasks:
            t.cancel()
        return bool(tasks)

    async def shutdown(self) -> None:
        tasks = [t for ts in self.tasks.values() for t in ts]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---- 晉級 ----

    def advance(self, exp_id: int, run_ids: list[int], user_turns: dict[int, str] | None = None) -> dict:
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
        new_ids = plan_stage(conn, exp, cur + 1, parents, user_turns)
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


# ---- 建立並開始（網頁、外部 agent 共用） ----

def create_and_start(
    conn: sqlite3.Connection, manager: "Manager", *, name: str, preset: Preset, script_id: int,
    scenario_ids: list[int], connection_id: int,
) -> int:
    from . import scripts as sc

    s = sc.get(conn, script_id)
    if s is None:
        raise ExperimentError(f"找不到腳本 #{script_id}")
    script, _warnings = sc.resolve(conn, script_id)
    exp_id = create(conn, name=name or f"{preset.name} × {s.name}", preset=preset, script_id=s.id, script_name=s.name,
                    script=script, scenario_ids=scenario_ids, connection_id=connection_id)
    manager.start_stage(exp_id, 0, plan_stage(conn, get_raw(conn, exp_id), 0, None))
    return exp_id


def create_rerun(conn: sqlite3.Connection, manager: "Manager", origin_id: int, preset: Preset, *,
                 name: str = "", connection_id: int | None = None) -> int:
    origin = get_raw(conn, origin_id)
    if origin is None:
        raise ExperimentError(f"找不到實驗 #{origin_id}")
    exp_id = create(
        conn, name=name or f"{origin['name']}（重跑）", preset=preset, script_id=origin["script_id"],
        script_name=origin["script_name"], script=ScriptData.model_validate(origin["script_snapshot"]),
        scenario_ids=origin["scenario_ids"], connection_id=connection_id or origin["connection_id"], rerun_of=origin_id,
    )
    manager.start_stage(exp_id, 0, plan_stage(conn, get_raw(conn, exp_id), 0, None))
    return exp_id


# ---- 重試原因與統計 ----

RETRY_REASONS_KEY = "retry_reasons"
DEFAULT_RETRY_REASONS = ["文風不佳", "格式 / 標籤錯誤", "出戲 / 拒答", "不符角色設定", "替使用者說話", "重複 / 套話", "太短 / 截斷"]


def load_retry_reasons(conn: sqlite3.Connection) -> list[str]:
    return (store.get_setting(conn, RETRY_REASONS_KEY) or {}).get("reasons") or DEFAULT_RETRY_REASONS


def retry_stats(conn: sqlite3.Connection, experiment_id: int | None = None) -> dict:
    """手動重試率依角色模型統計（使用者不滿意）；錯誤率依實際回應的供應商統計（上游穩定度）。"""
    where, args = ("WHERE r.experiment_id = ?", (experiment_id,)) if experiment_id is not None else ("WHERE r.experiment_id IS NOT NULL", ())
    gen = {}
    for row in conn.execute(
        f"""SELECT json_extract(r.connection, '$.model') AS model, COUNT(*) AS n FROM (
                SELECT run_id FROM run_messages WHERE role = 'assistant'
                UNION ALL SELECT run_id FROM run_message_archive WHERE role = 'assistant'
            ) m JOIN runs r ON r.id = m.run_id {where} GROUP BY model""",
        args,
    ):
        gen[row["model"]] = row["n"]
    lw, la = ("WHERE experiment_id = ?", (experiment_id,)) if experiment_id is not None else ("", ())
    models: dict[str, dict] = {}
    for row in conn.execute(f"SELECT model, kind, tags FROM retry_log {lw}", la):
        m = models.setdefault(row["model"] or "（未知）", {"manual": 0, "error": 0, "cascade": 0, "tags": {}})
        m[row["kind"]] = m.get(row["kind"], 0) + 1
        for tag in json.loads(row["tags"]):
            m["tags"][tag] = m["tags"].get(tag, 0) + 1
    out_models = []
    for model in sorted(set(gen) | set(models), key=str):
        m = models.get(model, {"manual": 0, "error": 0, "cascade": 0, "tags": {}})
        n = gen.get(model, 0)
        out_models.append({"model": model, "replies": n, **m, "manual_rate": round(m["manual"] / n, 3) if n else None})
    providers = []
    for row in conn.execute(
        f"""SELECT COALESCE(m.served_by, '（未取得）') AS provider, COUNT(*) AS n,
                   SUM(CASE WHEN m.error IS NOT NULL THEN 1 ELSE 0 END) AS errors
            FROM (SELECT run_id, served_by, error, cost FROM run_messages WHERE source IN ('char', 'llm_user', 'replay_fallback')
                  UNION ALL SELECT run_id, served_by, error, cost FROM run_message_archive WHERE source IN ('char', 'llm_user', 'replay_fallback')
            ) m JOIN runs r ON r.id = m.run_id {where} GROUP BY provider ORDER BY n DESC""",
        args,
    ):
        providers.append({"provider": row["provider"], "calls": row["n"], "errors": row["errors"],
                          "error_rate": round(row["errors"] / row["n"], 3) if row["n"] else None})
    return {"models": out_models, "providers": providers}


# ---- 迴歸試算（不寫入實驗） ----

def diff_workspace(conn: sqlite3.Connection, exp_id: int, preset: Preset, scenario_id: int | None = None) -> dict:
    """實驗用的版本 → 目前的預設（工作區）與角色卡。"""
    exp = get_raw(conn, exp_id)
    if exp is None:
        raise KeyError(f"找不到實驗 #{exp_id}")
    old = _preset(conn, exp["preset_hash"])
    same_preset = vcs.tree_id_of(old) == vcs.tree_id_of(preset)
    card = None
    if scenario_id is not None and str(scenario_id) in exp["state"].get("scenarios", {}):
        current = store.get_scenario(conn, scenario_id)
        if current is not None:
            card = revisions.diff(Scenario.model_validate(exp["state"]["scenarios"][str(scenario_id)]), current)
            # 測試夾具（歷史、使用者輸入…）不會用在試算 / 驗證，只列角色卡的差異
            card["fields"] = [f for f in card["fields"] if f["group"] == "card"]
    card_changed = bool(card and card["card_changed"])
    return {"same": same_preset and not card_changed, "same_preset": same_preset, "diff": vcs.diff_presets(old, preset),
            "card": card, "experiment_commit": exp["commit_id"], "preset_name": exp["preset_name"]}


def list_probes(conn: sqlite3.Connection, run_id: int, turn: int) -> list[dict]:
    defs = metrics_mod.load(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM probes WHERE run_id = ? AND turn = ? ORDER BY id DESC", (run_id, turn))]
    for r in rows:
        r["metrics"] = metrics_mod.measure(r["content"], defs)
    return rows


async def probe(conn: sqlite3.Connection, http: httpx.AsyncClient, exp_id: int, run_id: int, turn: int, preset: Preset,
                *, lookup_stats: bool = True):
    """用指定的預設（通常是修改後的工作區）重新產生某一回合的角色回覆：
    歷史與這一回合的使用者發言都和原分支相同，結果只存在 probes，不寫入實驗。產生 SSE 事件。"""
    from .build import build
    from .runner import sse

    exp = get_raw(conn, exp_id)
    run = store.get_run(conn, run_id)
    if exp is None or run is None or run["experiment_id"] != exp_id:
        raise KeyError("找不到這條分支")
    target = next((m for m in run["messages"] if m["turn"] == turn and m["role"] == "assistant"), None)
    user = next((m for m in run["messages"] if m["turn"] == turn and m["role"] == "user"), None)
    if user is None:
        raise ExperimentError(f"第 {turn} 回合沒有使用者發言，無法試算")
    connection, provider = provider_for(conn, exp["connection_id"], "角色")
    sc = revisions.with_current_card(Scenario.model_validate(run["scenario"]), store.get_scenario(conn, run["scenario_id"]))
    stop_idx = target["idx"] if target else user["idx"] + 1
    history = sc.chat() + [HistoryMessage(role=m["role"], content=m["content"]) for m in run["messages"]
                           if m["idx"] < stop_idx and not m["error"]]
    n = conn.execute("SELECT COUNT(*) FROM probes WHERE run_id = ? AND turn = ?", (run_id, turn)).fetchone()[0]
    built = build(preset, history, sc.names(), seed=run["seed"] + turn, card=sc.card, persona=sc.persona)
    body = request_body(built.to_api(), preset.params, connection, run["seed"] + turn + 7000 + n, provider,
                        temperature=run["temperature"])
    preset_hash = store.snapshot_preset(conn, preset)
    res = CallResult()
    yield sse({"type": "start", "warnings": built.warnings})
    async for ev in stream_call(http, provider, body, res, lookup_stats=lookup_stats):
        yield sse(ev)
    with conn:
        pid = conn.execute(
            """INSERT INTO probes (created_at, experiment_id, run_id, turn, preset_hash, commit_id, content, reasoning, cost,
                                   model, served_by, finish_reason, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (store.now_iso(), exp_id, run_id, turn, preset_hash, vcs.commit_for_tree(conn, vcs.tree_id_of(preset)),
             res.output, res.reasoning, res.cost, res.model, res.served_by, res.finish_reason, res.error),
        ).lastrowid
    yield sse({"type": "done", "probe": dict(conn.execute("SELECT * FROM probes WHERE id = ?", (pid,)).fetchone())})


# ---- 部分迴歸：只重跑指定階段的分支 ----

def create_verify(conn: sqlite3.Connection, manager: "Manager", origin_id: int, preset: Preset, *, from_stage: int,
                  run_ids: list[int] | None = None, name: str = "") -> int:
    """用新版本只重跑原實驗某一階段（全部或指定分支）。每條新分支沿用原分支的情境快照
    （含上層對話）與使用者發言，因此兩邊唯一的差別是預設；之前的階段不重跑。"""
    origin = get_raw(conn, origin_id)
    if origin is None:
        raise ExperimentError(f"找不到實驗 #{origin_id}")
    stages = origin["script_snapshot"]["stages"]
    if not 0 <= from_stage < len(stages):
        raise ExperimentError("階段超出範圍")
    src = [r for r in store.list_runs(conn, limit=10_000, experiment_id=origin_id) if r["stage"] == from_stage]
    if run_ids:
        src = [r for r in src if r["id"] in set(run_ids)]
    if not src:
        raise ExperimentError("沒有可以驗證的分支")
    provider_for(conn, origin["connection_id"], "角色")
    preset_hash = store.snapshot_preset(conn, preset)
    revs = {}
    for sid in origin["scenario_ids"]:
        current = store.get_scenario(conn, sid)
        if current is not None:
            revs[str(sid)] = revisions.ensure(conn, current, message="建立驗證輪次時自動存成版本")["id"]
    state = {"scenarios": origin["state"].get("scenarios", {}), "partial_from": from_stage, "scenario_revisions": revs}
    with conn:
        exp_id = conn.execute(
            """INSERT INTO experiments (created_at, name, status, current_stage, preset_name, preset_hash, commit_id,
                                        script_id, script_name, script_snapshot, script_hash, scenario_ids,
                                        connection_id, rerun_of, state)
               VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (store.now_iso(), name or f"{origin['name']}（驗證：階段 {from_stage + 1}）", from_stage, preset.name, preset_hash,
             vcs.commit_for_tree(conn, vcs.tree_id_of(preset)), origin["script_id"], origin["script_name"],
             json.dumps(origin["script_snapshot"], ensure_ascii=False), origin["script_hash"],
             json.dumps(origin["scenario_ids"]), origin["connection_id"], origin_id,
             json.dumps(state, ensure_ascii=False)),
        ).lastrowid
    connection, _ = provider_for(conn, origin["connection_id"], "角色")
    ids = []
    for s in sorted(src, key=lambda r: r["id"]):
        run = store.get_run(conn, s["id"])
        us = dict(run["user_settings"] or {})
        us["keep_turns"] = {str(m["turn"]): [m["content"], m["source"]] for m in run["messages"]
                            if m["role"] == "user" and not m["error"]}
        user_conn = Connection.model_validate(run["user_connection"]) if run["user_connection"] else None
        rid = store.create_run(
            conn, mode="live", replay_of=None, turns=run["turns"], seed=run["seed"], preset=preset, preset_hash=preset_hash,
            commit_id=vcs.commit_for_tree(conn, vcs.tree_id_of(preset)),
            scenario=revisions.with_current_card(Scenario.model_validate(run["scenario"]), store.get_scenario(conn, run["scenario_id"])),
            connection=connection, user_connection=user_conn, user_settings=us, status="queued", experiment_id=exp_id,
            stage=from_stage, parent_run_id=None, temperature=run["temperature"],
        )
        store.update_run(conn, rid, origin_run_id=s["id"])
        ids.append(rid)
    manager.start_stage(exp_id, from_stage, ids)
    return exp_id
