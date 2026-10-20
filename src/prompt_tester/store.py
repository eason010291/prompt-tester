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
    "id, created_at, status, preset_name, preset_hash, scenario_id, model, served_by, cost, cost_source, "
    "latency_ms, first_token_ms, finish_reason, error, substr(output, 1, 200) AS excerpt, "
    "json_extract(scenario, '$.name') AS scenario_name, json_extract(connection, '$.name') AS connection_name"
)


def create_generation(
    conn: sqlite3.Connection, *, preset: Preset, preset_hash: str, scenario: Scenario, connection: Connection, request: dict
) -> int:
    with conn:
        return conn.execute(
            """INSERT INTO generations (created_at, status, preset_name, preset_hash, scenario_id, scenario,
                                        connection, request, model)
               VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso(),
                preset.name,
                preset_hash,
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
        conn.execute("DELETE FROM generations WHERE id = ?", (gid,))


def mark_stale_running(conn: sqlite3.Connection) -> None:
    """伺服器重啟時，把上次沒有結束的生成標成中斷。"""
    with conn:
        conn.execute("UPDATE generations SET status = 'cancelled', error = '伺服器重啟時中斷' WHERE status = 'running'")

