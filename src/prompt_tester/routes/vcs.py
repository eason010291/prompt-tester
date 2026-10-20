"""預設版本樹 API：狀態、提交、log、切換、分支 / 標籤、diff。"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import vcs
from ..preset import Preset
from ..store import now_iso
from .deps import get_db
from .prompt import WORKSPACE_ID, get_workspace

router = APIRouter(prefix="/api/vcs")


def _head(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT branch, base_commit FROM workspaces WHERE id = ?", (WORKSPACE_ID,)).fetchone()
    if row is None:
        return vcs.DEFAULT_BRANCH, None
    return row["branch"], row["base_commit"]


def _working(conn: sqlite3.Connection) -> Preset:
    return get_workspace(conn).preset


def _save_working(conn: sqlite3.Connection, preset: Preset, branch: str | None, base: str | None) -> None:
    ws = get_workspace(conn)
    with conn:
        conn.execute(
            """INSERT INTO workspaces (id, preset, context, updated_at, branch, base_commit) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET preset = excluded.preset, updated_at = excluded.updated_at,
                                             branch = excluded.branch, base_commit = excluded.base_commit""",
            (WORKSPACE_ID, preset.model_dump_json(), ws.context.model_dump_json(), now_iso(), branch, base),
        )


def _error(e: vcs.VcsError, status: int = 422) -> HTTPException:
    return HTTPException(status, str(e))


class HeadState(BaseModel):
    branch: str | None
    base_commit: dict | None
    dirty: bool
    tree_id: str


@router.get("/state")
def state(conn: sqlite3.Connection = Depends(get_db)) -> HeadState:
    branch, base = _head(conn)
    tree_id = vcs.tree_id_of(_working(conn))
    commit = vcs.get_commit(conn, base) if base else None
    return HeadState(
        branch=branch,
        base_commit=commit.to_dict() if commit else None,
        dirty=commit is None or commit.tree_id != tree_id,
        tree_id=tree_id,
    )


class CommitIn(BaseModel):
    message: str
    # 先把這份內容存入工作區再提交，避免自動儲存尚未送出
    preset: Preset | None = None
    # detached 狀態下提交時，新建的分支名稱
    branch: str | None = None


@router.post("/commit")
def commit(body: CommitIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    branch, base = _head(conn)
    preset = body.preset or _working(conn)
    target = branch or (body.branch or "").strip()
    if not target:
        raise HTTPException(422, "目前不在任何分支上（detached），請提供新分支名稱")
    if branch:
        ref = vcs.get_ref(conn, branch)
        if ref and ref["commit_id"] != base:
            raise HTTPException(409, f"分支「{branch}」已經前進到 {ref['commit_id'][:8]}，請先切換到最新版本")
    if base and vcs.get_commit(conn, base).tree_id == vcs.tree_id_of(preset):
        raise HTTPException(422, "沒有變更可以提交")
    try:
        c = vcs.create_commit(conn, preset, [base] if base else [], body.message)
        existed = vcs.get_ref(conn, target) is not None
        vcs.set_ref(conn, target, c.id, "branch", protected=None if existed else target == vcs.DEFAULT_BRANCH)
    except vcs.VcsError as e:
        raise _error(e) from e
    _save_working(conn, preset, target, c.id)
    return c.to_dict()


@router.get("/log")
def log(limit: int = 500, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    branch, base = _head(conn)
    counts = {
        r["commit_id"]: r["n"]
        for r in conn.execute("SELECT commit_id, COUNT(*) AS n FROM generations WHERE commit_id IS NOT NULL GROUP BY commit_id")
    }
    return {
        "commits": [c.to_dict() | {"generations": counts.get(c.id, 0)} for c in vcs.log(conn, min(limit, 2000))],
        "refs": vcs.list_refs(conn),
        "head": {"branch": branch, "base_commit": base},
    }


@router.get("/commits/{cid}/preset")
def commit_preset(cid: str, conn: sqlite3.Connection = Depends(get_db)) -> Preset:
    try:
        return vcs.read_tree(conn, vcs.get_commit(conn, cid).tree_id)
    except vcs.VcsError as e:
        raise _error(e, 404) from e


class CheckoutIn(BaseModel):
    # 分支名稱、標籤名稱或 commit id（可用前綴）
    target: str
    # 捨棄未提交的變更
    force: bool = False


@router.post("/checkout")
def checkout(body: CheckoutIn, conn: sqlite3.Connection = Depends(get_db)) -> HeadState:
    current = state(conn)
    ref = vcs.get_ref(conn, body.target)
    try:
        commit = vcs.get_commit(conn, ref["commit_id"] if ref else body.target)
    except vcs.VcsError as e:
        raise _error(e, 404) from e
    new_branch = body.target if ref and ref["kind"] == "branch" else None
    if current.dirty and current.base_commit is not None and not body.force:
        raise HTTPException(409, "工作區有未提交的變更；請先提交，或選擇捨棄變更後切換")
    preset = vcs.read_tree(conn, commit.tree_id, collapsed_from=_working(conn))
    _save_working(conn, preset, new_branch, commit.id)
    return state(conn)


class RefIn(BaseModel):
    name: str
    kind: str = "branch"
    # 預設為目前的基準 commit
    commit_id: str | None = None
    # 建立分支後切換過去（基準相同時保留未提交的變更，如同 git switch -c）
    checkout: bool = False


@router.post("/refs")
def create_ref(body: RefIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    branch, base = _head(conn)
    if body.kind not in ("branch", "tag"):
        raise HTTPException(422, "kind 必須是 branch 或 tag")
    target = body.commit_id or base
    if not target:
        raise HTTPException(422, "還沒有任何提交，請先提交一次")
    try:
        commit = vcs.get_commit(conn, target)
        if body.kind == "branch" and vcs.get_ref(conn, body.name):
            raise vcs.VcsError(f"分支「{body.name}」已存在")
        vcs.set_ref(conn, body.name, commit.id, body.kind, protected=False)
    except vcs.VcsError as e:
        raise _error(e) from e
    if body.kind == "branch" and body.checkout:
        if commit.id == base:
            with conn:
                conn.execute("UPDATE workspaces SET branch = ? WHERE id = ?", (body.name.strip(), WORKSPACE_ID))
        else:
            checkout(CheckoutIn(target=body.name), conn)
    return vcs.get_ref(conn, body.name.strip()) or {}


@router.delete("/refs/{name}", status_code=204)
def delete_ref(name: str, conn: sqlite3.Connection = Depends(get_db)) -> None:
    branch, _ = _head(conn)
    try:
        vcs.delete_ref(conn, name, branch)
    except vcs.VcsError as e:
        raise _error(e) from e


def _resolve(conn: sqlite3.Connection, spec: str) -> Preset:
    if spec == "WORKING":
        return _working(conn)
    if spec == "EMPTY":
        return Preset(name="")
    ref = vcs.get_ref(conn, spec)
    return vcs.read_tree(conn, vcs.get_commit(conn, ref["commit_id"] if ref else spec).tree_id)


@router.get("/diff")
def diff(a: str, b: str = "WORKING", conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """a / b：commit id、分支 / 標籤名稱、WORKING（工作區）或 EMPTY。"""
    try:
        return vcs.diff_presets(_resolve(conn, a), _resolve(conn, b))
    except vcs.VcsError as e:
        raise _error(e, 404) from e
