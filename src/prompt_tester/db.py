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
    (
        4,
        # M3：預設版本庫（內容定址 blob / tree / commit 與 ref），工作區掛在分支上
        """
        CREATE TABLE blobs (
            id   TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE trees (
            id   TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE commits (
            id         TEXT PRIMARY KEY,
            tree_id    TEXT NOT NULL REFERENCES trees(id),
            parents    TEXT NOT NULL,
            author     TEXT NOT NULL,
            message    TEXT NOT NULL,
            meta       TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX commits_tree ON commits(tree_id);
        CREATE TABLE refs (
            name       TEXT PRIMARY KEY,
            kind       TEXT NOT NULL CHECK (kind IN ('branch', 'tag')),
            commit_id  TEXT REFERENCES commits(id),
            protected  INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        ALTER TABLE workspaces ADD COLUMN branch TEXT DEFAULT 'main';  -- NULL = detached
        ALTER TABLE workspaces ADD COLUMN base_commit TEXT;
        ALTER TABLE generations ADD COLUMN commit_id TEXT;
        """,
    ),
    (
        5,
        # 多回合試跑：LLM 扮演 {{user}}（即時）或回放舊紀錄
        """
        CREATE TABLE runs (
            id              INTEGER PRIMARY KEY,
            created_at      TEXT NOT NULL,
            status          TEXT NOT NULL,       -- running | done | error | cancelled
            mode            TEXT NOT NULL,       -- live | replay
            replay_of       INTEGER,
            turns           INTEGER NOT NULL,
            seed            INTEGER NOT NULL,
            preset_name     TEXT NOT NULL,
            preset_hash     TEXT NOT NULL REFERENCES preset_snapshots(hash),
            commit_id       TEXT,
            scenario_id     INTEGER,
            scenario        TEXT NOT NULL,
            connection      TEXT NOT NULL,
            user_connection TEXT,
            user_settings   TEXT,
            cost            REAL,
            checks          TEXT,
            error           TEXT
        );
        CREATE INDEX runs_scenario ON runs(scenario_id, id DESC);
        CREATE TABLE run_messages (
            id             INTEGER PRIMARY KEY,
            run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            idx            INTEGER NOT NULL,
            turn           INTEGER NOT NULL,
            role           TEXT NOT NULL,        -- user | assistant
            source         TEXT NOT NULL,        -- scenario | llm_user | replay | replay_fallback | char
            content        TEXT NOT NULL,
            reasoning      TEXT NOT NULL DEFAULT '',
            usage          TEXT,
            cost           REAL,
            cost_source    TEXT,
            served_by      TEXT,
            model          TEXT,
            first_token_ms INTEGER,
            latency_ms     INTEGER,
            finish_reason  TEXT,
            error          TEXT,
            request        TEXT
        );
        CREATE INDEX run_messages_run ON run_messages(run_id, idx);
        """,
    ),
    (
        6,
        # M5：人工評語（自由文字 + 解析出的分數）
        """
        CREATE TABLE feedback (
            id          INTEGER PRIMARY KEY,
            target_type TEXT NOT NULL CHECK (target_type IN ('run', 'run_message', 'generation')),
            target_id   INTEGER NOT NULL,
            run_id      INTEGER,               -- 試跑或其訊息的評語，方便彙總
            text        TEXT NOT NULL,
            scores      TEXT NOT NULL,         -- {"屬性": 分數}
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX feedback_target ON feedback(target_type, target_id);
        CREATE INDEX feedback_run ON feedback(run_id);
        """,
    ),
    (
        7,
        # M4：腳本繼承樹、金字塔實驗。每條分支是一筆 run，晉級後的接續以 parent_run_id 串成對話樹
        """
        CREATE TABLE scripts (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            parent_id  INTEGER REFERENCES scripts(id),
            overrides  TEXT NOT NULL,          -- {"stages[0].seeds": 6, ...}
            notes      TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE experiments (
            id              INTEGER PRIMARY KEY,
            created_at      TEXT NOT NULL,
            name            TEXT NOT NULL,
            status          TEXT NOT NULL,     -- running | waiting | done | cancelled
            current_stage   INTEGER NOT NULL DEFAULT 0,
            preset_name     TEXT NOT NULL,
            preset_hash     TEXT NOT NULL REFERENCES preset_snapshots(hash),
            commit_id       TEXT,
            script_id       INTEGER,
            script_name     TEXT NOT NULL,
            script_snapshot TEXT NOT NULL,     -- 解析後的完整腳本
            script_hash     TEXT NOT NULL,
            scenario_ids    TEXT NOT NULL,
            connection_id   INTEGER NOT NULL,
            rerun_of        INTEGER,
            state           TEXT NOT NULL DEFAULT '{}',   -- 各階段的評審結果等
            cost            REAL NOT NULL DEFAULT 0,
            error           TEXT
        );
        ALTER TABLE runs ADD COLUMN experiment_id INTEGER;
        ALTER TABLE runs ADD COLUMN stage INTEGER;
        ALTER TABLE runs ADD COLUMN parent_run_id INTEGER;
        ALTER TABLE runs ADD COLUMN temperature REAL;
        ALTER TABLE runs ADD COLUMN promoted INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX runs_experiment ON runs(experiment_id, stage);
        """,
    ),
    (
        8,
        # M6：LLM 依評語修改預設（內建 tool-calling 迴圈）的每一輪紀錄
        """
        CREATE TABLE agent_rounds (
            id                  INTEGER PRIMARY KEY,
            created_at          TEXT NOT NULL,
            status              TEXT NOT NULL,   -- running | done | uncommitted | error | cancelled
            author              TEXT NOT NULL,
            connection          TEXT NOT NULL,
            instructions        TEXT NOT NULL,
            experiment_id       INTEGER,
            auto_rerun          INTEGER NOT NULL DEFAULT 0,
            base_commit         TEXT,
            branch              TEXT,
            result_commit       TEXT,
            rerun_experiment_id INTEGER,
            steps               TEXT NOT NULL DEFAULT '[]',   -- 每次 LLM 呼叫：文字、工具呼叫與結果、費用
            final_message       TEXT,
            tool_calls          INTEGER NOT NULL DEFAULT 0,
            cost                REAL NOT NULL DEFAULT 0,
            error               TEXT
        );
        CREATE INDEX agent_rounds_experiment ON agent_rounds(experiment_id);
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
