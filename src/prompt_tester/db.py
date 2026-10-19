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
