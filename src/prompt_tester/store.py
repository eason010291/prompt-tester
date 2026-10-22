"""資料存取：情境、連線、預設快照、生成紀錄。"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from .entities import Connection, Scenario
from .preset import LegacyPreviewContext, Preset


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---- 情境 ----

def list_scenarios(conn: sqlite3.Connection) -> list[Scenario]:
    rows = conn.execute("SELECT id, data, updated_at FROM scenarios ORDER BY id").fetchall()
    return [_scenario(r) for r in rows]


def get_scenario(conn: sqlite3.Connection, sid: int) -> Scenario | None:
    row = conn.execute("SELECT id, data, updated_at FROM scenarios WHERE id = ?", (sid,)).fetchone()
    return _scenario(row) if row else None


def _scenario(row) -> Scenario:
    s = Scenario.model_validate_json(row["data"])
    s.id, s.updated_at = row["id"], row["updated_at"]
    return s


def save_scenario(conn: sqlite3.Connection, s: Scenario) -> Scenario:
    data = s.model_dump_json(exclude={"id", "updated_at"})
    ts = now_iso()
    with conn:
        if s.id is None:
            s.id = conn.execute("INSERT INTO scenarios (data, updated_at) VALUES (?, ?)", (data, ts)).lastrowid
        else:
            cur = conn.execute("UPDATE scenarios SET data = ?, updated_at = ? WHERE id = ?", (data, ts, s.id))
            if cur.rowcount == 0:
                raise KeyError(s.id)
    s.updated_at = ts
    return s


def delete_scenario(conn: sqlite3.Connection, sid: int) -> None:
    with conn:
        conn.execute("DELETE FROM scenarios WHERE id = ?", (sid,))
        conn.execute("DELETE FROM scenario_revisions WHERE scenario_id = ?", (sid,))


def starter_scenario() -> Scenario:
    return Scenario(
        name="範例情境",
        opening="（{{char}} 抬起頭）你來了。",
        user_turns=["嗯，今天有點累。", "可以陪我聊一下嗎？"],
    )


def scenario_from_legacy(legacy: LegacyPreviewContext) -> Scenario:
    history = list(legacy.history)
    opening = ""
    if history and history[0].role == "assistant":
        opening = history.pop(0).content
    return Scenario(name="預覽情境（自 M1 轉入）", char=legacy.char, user=legacy.user, opening=opening, history=history)


# ---- 連線 ----

def list_connections(conn: sqlite3.Connection) -> list[Connection]:
    return [_connection(r) for r in conn.execute("SELECT id, data FROM connections ORDER BY id").fetchall()]


def get_connection(conn: sqlite3.Connection, cid: int) -> Connection | None:
    row = conn.execute("SELECT id, data FROM connections WHERE id = ?", (cid,)).fetchone()
    return _connection(row) if row else None


def _connection(row) -> Connection:
    c = Connection.model_validate_json(row["data"])
    c.id = row["id"]
    return c


def save_connection(conn: sqlite3.Connection, c: Connection) -> Connection:
    data = c.model_dump_json(exclude={"id"})
    with conn:
        if c.id is None:
            c.id = conn.execute("INSERT INTO connections (data) VALUES (?)", (data,)).lastrowid
        elif conn.execute("UPDATE connections SET data = ? WHERE id = ?", (data, c.id)).rowcount == 0:
            raise KeyError(c.id)
    return c


def delete_connection(conn: sqlite3.Connection, cid: int) -> None:
    with conn:
        conn.execute("DELETE FROM connections WHERE id = ?", (cid,))


# ---- 預設快照（內容定址） ----

def snapshot_preset(conn: sqlite3.Connection, preset: Preset) -> str:
    # 收合狀態只是 UI，不影響內容
    data = preset.model_dump(mode="json")
    for b in data["blocks"]:
        b.pop("collapsed", None)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    with conn:
        conn.execute("INSERT OR IGNORE INTO preset_snapshots (hash, data) VALUES (?, ?)", (digest, text))
    return digest


# ---- 生成紀錄 ----

GENERATION_SUMMARY = (
    "id, created_at, status, preset_name, preset_hash, commit_id, scenario_id, model, served_by, cost, cost_source, "
    "latency_ms, first_token_ms, finish_reason, error, substr(output, 1, 200) AS excerpt, "
    "json_extract(scenario, '$.name') AS scenario_name, json_extract(connection, '$.name') AS connection_name"
)


def create_generation(
    conn: sqlite3.Connection,
    *,
    preset: Preset,
    preset_hash: str,
    scenario: Scenario,
    connection: Connection,
    request: dict,
    commit_id: str | None = None,
) -> int:
    with conn:
        return conn.execute(
            """INSERT INTO generations (created_at, status, preset_name, preset_hash, commit_id, scenario_id, scenario,
                                        connection, request, model)
               VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso(),
                preset.name,
                preset_hash,
                commit_id,
                scenario.id,
                scenario.model_dump_json(),
                connection.model_dump_json(),
                json.dumps(request, ensure_ascii=False),
                connection.model,
            ),
        ).lastrowid


def finish_generation(conn: sqlite3.Connection, gid: int, **fields) -> None:
    for key in ("usage",):
        if key in fields and fields[key] is not None:
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE generations SET {cols} WHERE id = ?", (*fields.values(), gid))


def list_generations(conn: sqlite3.Connection, limit: int = 30, scenario_id: int | None = None) -> list[dict]:
    sql = f"SELECT {GENERATION_SUMMARY} FROM generations"
    args: tuple = ()
    if scenario_id is not None:
        sql += " WHERE scenario_id = ?"
        args = (scenario_id,)
    sql += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*args, limit)).fetchall()]


def get_generation(conn: sqlite3.Connection, gid: int) -> dict | None:
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (gid,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in ("scenario", "connection", "request", "usage"):
        if out.get(key):
            out[key] = json.loads(out[key])
    return out


def delete_generation(conn: sqlite3.Connection, gid: int) -> None:
    with conn:
        conn.execute("DELETE FROM feedback WHERE target_type = 'generation' AND target_id = ?", (gid,))
        conn.execute("DELETE FROM generations WHERE id = ?", (gid,))


def mark_stale_running(conn: sqlite3.Connection) -> None:
    """伺服器重啟時，把上次沒有結束的生成與試跑標成中斷。"""
    with conn:
        conn.execute("UPDATE generations SET status = 'cancelled', error = '伺服器重啟時中斷' WHERE status = 'running'")
        conn.execute("UPDATE runs SET status = 'cancelled', error = '伺服器重啟時中斷' WHERE status IN ('running', 'queued')")
        conn.execute("UPDATE experiments SET status = 'cancelled', error = '伺服器重啟時中斷' WHERE status = 'running'")


# ---- 鍵值設定 ----

def get_setting(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: dict) -> None:
    with conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ---- 多回合試跑 ----

def create_run(
    conn: sqlite3.Connection,
    *,
    mode: str,
    replay_of: int | None,
    turns: int,
    seed: int,
    preset: Preset,
    preset_hash: str,
    commit_id: str | None,
    scenario: Scenario,
    connection: Connection,
    user_connection: Connection | None,
    user_settings: dict | None,
    status: str = "running",
    experiment_id: int | None = None,
    stage: int | None = None,
    parent_run_id: int | None = None,
    temperature: float | None = None,
) -> int:
    with conn:
        return conn.execute(
            """INSERT INTO runs (created_at, status, mode, replay_of, turns, seed, preset_name, preset_hash, commit_id,
                                 scenario_id, scenario, connection, user_connection, user_settings,
                                 experiment_id, stage, parent_run_id, temperature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso(),
                status,
                mode,
                replay_of,
                turns,
                seed,
                preset.name,
                preset_hash,
                commit_id,
                scenario.id,
                scenario.model_dump_json(),
                connection.model_dump_json(),
                user_connection.model_dump_json() if user_connection else None,
                json.dumps(user_settings, ensure_ascii=False) if user_settings is not None else None,
                experiment_id,
                stage,
                parent_run_id,
                temperature,
            ),
        ).lastrowid


def add_run_message(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    idx: int,
    turn: int,
    role: str,
    source: str,
    content: str,
    result,
    request: dict | None,
) -> dict:
    r = result
    with conn:
        mid = conn.execute(
            """INSERT INTO run_messages (run_id, idx, turn, role, source, content, reasoning, usage, cost, cost_source,
                                         served_by, model, first_token_ms, latency_ms, finish_reason, error, request)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                idx,
                turn,
                role,
                source,
                content,
                r.reasoning if r else "",
                json.dumps(r.usage) if r and r.usage else None,
                r.cost if r else None,
                r.cost_source if r else None,
                r.served_by if r else None,
                r.model if r else None,
                r.first_token_ms if r else None,
                r.latency_ms if r else None,
                r.finish_reason if r else None,
                r.error if r else None,
                json.dumps(request, ensure_ascii=False) if request else None,
            ),
        ).lastrowid
    row = conn.execute("SELECT * FROM run_messages WHERE id = ?", (mid,)).fetchone()
    return _run_message(row, with_request=False)


def _run_message(row, with_request: bool) -> dict:
    d = dict(row)
    d["usage"] = json.loads(d["usage"]) if d["usage"] else None
    if with_request:
        d["request"] = json.loads(d["request"]) if d["request"] else None
    else:
        d.pop("request", None)
    return d


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, error: str | None, cost: float, checks: dict) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET status = ?, error = ?, cost = ?, checks = ? WHERE id = ?",
            (status, error, cost, json.dumps(checks, ensure_ascii=False), run_id),
        )


RUN_SUMMARY = """SELECT r.id, r.created_at, r.status, r.mode, r.replay_of, r.turns, r.seed, r.preset_name, r.preset_hash,
                    r.commit_id, r.scenario_id, json_extract(r.scenario, '$.name') AS scenario_name, r.cost, r.checks, r.error,
                    json_extract(r.connection, '$.name') AS connection_name,
                    json_extract(r.user_connection, '$.name') AS user_connection_name,
                    r.experiment_id, r.stage, r.parent_run_id, r.temperature, r.promoted, r.origin_run_id,
                    (SELECT COUNT(*) FROM run_messages m WHERE m.run_id = r.id AND m.role = 'assistant') AS replies,
                    (SELECT MAX(turn) FROM run_messages m WHERE m.run_id = r.id AND m.role = 'assistant'
                       AND m.error IS NULL) AS last_turn
             FROM runs r"""


def list_runs(
    conn: sqlite3.Connection,
    scenario_id: int | None = None,
    limit: int = 30,
    *,
    experiment_id: int | None = None,
    standalone: bool = False,
) -> list[dict]:
    """standalone：只列不屬於實驗的試跑（情境頁用）。"""
    where, args = [], []
    if scenario_id is not None:
        where.append("r.scenario_id = ?")
        args.append(scenario_id)
    if experiment_id is not None:
        where.append("r.experiment_id = ?")
        args.append(experiment_id)
    if standalone:
        where.append("r.experiment_id IS NULL")
    sql = RUN_SUMMARY + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.id DESC LIMIT ?"
    out = []
    for row in conn.execute(sql, (*args, limit)).fetchall():
        d = dict(row)
        d["checks"] = json.loads(d["checks"]) if d["checks"] else None
        out.append(d)
    return out


def get_run(conn: sqlite3.Connection, run_id: int, with_requests: bool = False) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("scenario", "connection", "user_connection", "user_settings", "checks"):
        if d.get(key):
            d[key] = json.loads(d[key])
    d["messages"] = [
        _run_message(m, with_requests)
        for m in conn.execute("SELECT * FROM run_messages WHERE run_id = ? ORDER BY idx", (run_id,)).fetchall()
    ]
    return d


def run_user_turns(conn: sqlite3.Connection, run_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM run_messages WHERE run_id = ? AND role = 'user' ORDER BY turn", (run_id,)
    ).fetchall()
    return [r["content"] for r in rows]


def truncate_run(conn: sqlite3.Connection, run_id: int, from_idx: int, reason: str) -> None:
    """重試前：把 idx >= from_idx 的訊息封存並刪除（連同掛在這些訊息上的評語）。"""
    ts = now_iso()
    with conn:
        conn.execute(
            """INSERT INTO run_message_archive (run_id, idx, turn, role, source, content, cost, model, archived_at, reason,
                                               error, served_by)
               SELECT run_id, idx, turn, role, source, content, cost, model, ?, ?, error, served_by FROM run_messages
               WHERE run_id = ? AND idx >= ?""",
            (ts, reason, run_id, from_idx),
        )
        conn.execute(
            """DELETE FROM feedback WHERE target_type = 'run_message'
               AND target_id IN (SELECT id FROM run_messages WHERE run_id = ? AND idx >= ?)""",
            (run_id, from_idx),
        )
        conn.execute("DELETE FROM run_messages WHERE run_id = ? AND idx >= ?", (run_id, from_idx))


def update_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*vals, run_id))


def run_archive(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM run_message_archive WHERE run_id = ? ORDER BY archived_at DESC, idx", (run_id,)
    ).fetchall()]


def delete_run(conn: sqlite3.Connection, run_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM feedback WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_messages WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_message_archive WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
