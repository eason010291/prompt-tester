"""工作區（掛在分支上的 working copy）的讀寫與提交。網頁的版本 API 與 LLM 修改工具共用。"""

import sqlite3

from . import vcs
from .preset import Preset
from .routes.prompt import WORKSPACE_ID, get_workspace
from .store import now_iso


def head(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(分支, 基準 commit)。分支為 None 表示 detached。"""
    row = conn.execute("SELECT branch, base_commit FROM workspaces WHERE id = ?", (WORKSPACE_ID,)).fetchone()
    if row is None:
        return vcs.DEFAULT_BRANCH, None
    return row["branch"], row["base_commit"]


def working(conn: sqlite3.Connection) -> Preset:
    return get_workspace(conn).preset


def save(conn: sqlite3.Connection, preset: Preset, branch: str | None, base: str | None) -> None:
    ws = get_workspace(conn)
    with conn:
        conn.execute(
            """INSERT INTO workspaces (id, preset, context, updated_at, branch, base_commit) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET preset = excluded.preset, updated_at = excluded.updated_at,
                                             branch = excluded.branch, base_commit = excluded.base_commit""",
            (WORKSPACE_ID, preset.model_dump_json(), ws.context.model_dump_json(), now_iso(), branch, base),
        )


def save_preset(conn: sqlite3.Connection, preset: Preset) -> None:
    """只改內容，分支與基準不變。"""
    branch, base = head(conn)
    save(conn, preset, branch, base)


def is_dirty(conn: sqlite3.Connection) -> bool:
    _, base = head(conn)
    return base is None or vcs.get_commit(conn, base).tree_id != vcs.tree_id_of(working(conn))


def commit(
    conn: sqlite3.Connection,
    message: str,
    *,
    preset: Preset | None = None,
    new_branch: str | None = None,
    author: str = "human",
    meta: dict | None = None,
) -> vcs.Commit:
    """提交工作區。detached 時需要 new_branch；分支被別處推進時拒絕（避免覆蓋）。"""
    branch, base = head(conn)
    preset = preset or working(conn)
    target = branch or (new_branch or "").strip()
    if not target:
        raise vcs.VcsError("目前不在任何分支上（detached），請提供新分支名稱")
    if branch:
        ref = vcs.get_ref(conn, branch)
        if ref and ref["commit_id"] != base:
            raise vcs.ConflictError(f"分支「{branch}」已經前進到 {ref['commit_id'][:8]}，請先切換到最新版本")
    if base and vcs.get_commit(conn, base).tree_id == vcs.tree_id_of(preset):
        raise vcs.VcsError("沒有變更可以提交")
    c = vcs.create_commit(conn, preset, [base] if base else [], message, author=author, meta=meta)
    existed = vcs.get_ref(conn, target) is not None
    vcs.set_ref(conn, target, c.id, "branch", protected=None if existed else target == vcs.DEFAULT_BRANCH)
    save(conn, preset, target, c.id)
    return c


def ensure_unprotected_branch(conn: sqlite3.Connection, prefix: str) -> str:
    """LLM / 外部 agent 不能推進受保護的分支：目前在受保護分支或 detached 時，從基準建立新分支並切換過去。"""
    branch, base = head(conn)
    if branch:
        ref = vcs.get_ref(conn, branch)
        if ref is None or not ref["protected"]:
            return branch
    if base is None:
        raise vcs.VcsError("還沒有任何提交；請先在網頁上提交一次，作為修改的基準")
    stamp = now_iso()[:16].replace("-", "").replace(":", "").replace("T", "-")
    name, n = f"{prefix}/{stamp}", 1
    while vcs.get_ref(conn, name):
        n += 1
        name = f"{prefix}/{stamp}-{n}"
    vcs.set_ref(conn, name, base, "branch", protected=False)
    with conn:
        conn.execute("UPDATE workspaces SET branch = ? WHERE id = ?", (name, WORKSPACE_ID))
    return name
