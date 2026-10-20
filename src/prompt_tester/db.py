"""SQLite 連線與遷移。每個里程碑新增的資料表以一筆 migration 追加，不修改舊的。"""

import sqlite3
from pathlib import Path

# (版本號, SQL)。只能往後追加。
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        # M1：編輯中的預設與預覽情境。M3 起成為分支上的工作區（working copy）。
        """
        CREATE TABLE workspaces (
            id         INTEGER PRIMARY KEY,
            preset     TEXT NOT NULL,
            context    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        3,
        # M2：情境、連線、預設快照（內容定址，M3 版本樹沿用）、單次生成紀錄
        """
        CREATE TABLE scenarios (
            id         INTEGER PRIMARY KEY,
            data       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE connections (
            id   INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE preset_snapshots (
            hash TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE generations (
            id             INTEGER PRIMARY KEY,
            created_at     TEXT NOT NULL,
            status         TEXT NOT NULL,          -- running | done | error | cancelled
            preset_name    TEXT NOT NULL,
            preset_hash    TEXT NOT NULL REFERENCES preset_snapshots(hash),
            scenario_id    INTEGER,
            scenario       TEXT NOT NULL,          -- 情境快照
            connection     TEXT NOT NULL,          -- 連線快照
            request        TEXT NOT NULL,          -- 實際送出的 body（不含金鑰）
            output         TEXT NOT NULL DEFAULT '',
            reasoning      TEXT NOT NULL DEFAULT '',
            finish_reason  TEXT,
            usage          TEXT,
            cost           REAL,
            cost_source    TEXT,                   -- api | generation | estimate
            served_by      TEXT,
            model          TEXT,
            remote_id      TEXT,
            first_token_ms INTEGER,
            latency_ms     INTEGER,
            error          TEXT
        );
        CREATE INDEX generations_created ON generations(created_at DESC);
        """,
    ),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
        current = version
    return current
