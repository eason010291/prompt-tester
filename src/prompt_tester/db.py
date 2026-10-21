"""SQLite 連線與遷移。每個里程碑新增的資料表以一筆 migration 追加，不修改舊的。"""

import sqlite3
import threading
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
    (
        9,
        # 重試：被取代的訊息搬到封存表（不刪除），可在分支檢視中查看舊版
        """
        CREATE TABLE run_message_archive (
            id          INTEGER PRIMARY KEY,
            run_id      INTEGER NOT NULL,
            idx         INTEGER NOT NULL,
            turn        INTEGER NOT NULL,
            role        TEXT NOT NULL,
            source      TEXT NOT NULL,
            content     TEXT NOT NULL,
            cost        REAL,
            model       TEXT,
            archived_at TEXT NOT NULL,
            reason      TEXT NOT NULL
        );
        CREATE INDEX run_message_archive_run ON run_message_archive(run_id, idx);
        """,
    ),
    (
        10,
        # 分層重試：重試紀錄（原因標籤）、封存保留錯誤與供應商（統計用）；
        # 部分迴歸：新分支記下對照的原分支；迴歸試算（不寫入實驗）
        """
        CREATE TABLE retry_log (
            id            INTEGER PRIMARY KEY,
            created_at    TEXT NOT NULL,
            experiment_id INTEGER NOT NULL,
            run_id        INTEGER NOT NULL,
            scope         TEXT NOT NULL,      -- message | branch | stage | resume | cascade
            kind          TEXT NOT NULL,      -- manual（使用者不滿意）| error（出錯 / 中斷）| cascade（上層連帶）
            turn          INTEGER,
            role          TEXT,
            model         TEXT,               -- 角色連線的模型
            tags          TEXT NOT NULL DEFAULT '[]',
            note          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX retry_log_experiment ON retry_log(experiment_id);
        ALTER TABLE run_message_archive ADD COLUMN error TEXT;
        ALTER TABLE run_message_archive ADD COLUMN served_by TEXT;
        ALTER TABLE runs ADD COLUMN origin_run_id INTEGER;
        CREATE TABLE probes (
            id            INTEGER PRIMARY KEY,
            created_at    TEXT NOT NULL,
            experiment_id INTEGER NOT NULL,
            run_id        INTEGER NOT NULL,
            turn          INTEGER NOT NULL,
            preset_hash   TEXT NOT NULL,
            commit_id     TEXT,
            content       TEXT NOT NULL,
            reasoning     TEXT NOT NULL DEFAULT '',
            cost          REAL,
            model         TEXT,
            served_by     TEXT,
            finish_reason TEXT,
            error         TEXT
        );
        CREATE INDEX probes_run ON probes(run_id, turn);
        """,
    ),
]


# 寫入互相等待的上限（毫秒）。交易都很短，正常不會等到
BUSY_TIMEOUT_MS = 10_000


class ThreadLocalConnection:
    """每個執行緒各自一條 SQLite 連線，介面與 sqlite3.Connection 相同（execute、with 交易…）。

    FastAPI 的同步 endpoint 在執行緒池執行，async endpoint 與背景工作在事件迴圈執行緒；
    共用同一條連線時，並行請求會互相干擾（sqlite3.InterfaceError: bad parameter or other API misuse）。
    WAL 模式下各連線可同時讀，寫入以 busy_timeout 排隊。
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    def __getattr__(self, name):
        return getattr(self._conn(), name)

    def __enter__(self):
        return self._conn().__enter__()

    def __exit__(self, *exc):
        return self._conn().__exit__(*exc)

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                c.close()
            self._all.clear()
        self._local = threading.local()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = ThreadLocalConnection(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn  # type: ignore[return-value]  # 介面相容


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
