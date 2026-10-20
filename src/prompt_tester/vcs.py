"""預設的類 git 版本庫（design.md §6）。

物件皆以正規化 JSON 的 sha256 定址、不可變、自動去重：
- blob：單一區塊（不含 enabled 與 UI 的 collapsed），或預設的 extra（常含大型正則 CSS）
- tree：{name, blocks: [{blob, enabled}], params, options, extra: blob}
- commit：{tree, parents, author, message, created_at, meta}
- ref：分支（可移動）或標籤，指向 commit；protected 的分支不允許 LLM 移動（M6）

工作區掛在一個分支上（workspaces.branch；NULL 表示 detached），base_commit 是工作區的基準。
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .preset import Block, Options, Params, Preset
from .store import now_iso

DEFAULT_BRANCH = "main"
BLOCK_STATE_FIELDS = {"enabled", "collapsed"}


class VcsError(ValueError):
    pass


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _put(conn: sqlite3.Connection, table: str, obj) -> str:
    text = _canon(obj)
    oid = _hash(text)
    conn.execute(f"INSERT OR IGNORE INTO {table} (id, data) VALUES (?, ?)", (oid, text))
    return oid


def _get(conn: sqlite3.Connection, table: str, oid: str):
    row = conn.execute(f"SELECT data FROM {table} WHERE id = ?", (oid,)).fetchone()
    if row is None:
        raise VcsError(f"找不到物件 {table}/{oid[:8]}")
    return json.loads(row["data"])


# ---- 預設 ↔ tree ----

def tree_object(preset: Preset) -> tuple[dict, list[dict], dict]:
    """回傳 (tree, 區塊 blobs, extra)；不寫入資料庫，供計算 tree id。"""
    blocks = []
    for b in preset.blocks:
        d = b.model_dump(mode="json")
        blocks.append({k: v for k, v in d.items() if k not in BLOCK_STATE_FIELDS})
    extra = preset.extra
    tree = {
        "name": preset.name,
        "blocks": [{"blob": _hash(_canon(bd)), "enabled": b.enabled} for bd, b in zip(blocks, preset.blocks)],
        "params": preset.params.model_dump(mode="json"),
        "options": preset.options.model_dump(mode="json"),
        "extra": _hash(_canon(extra)),
    }
    return tree, blocks, extra


def tree_id_of(preset: Preset) -> str:
    return _hash(_canon(tree_object(preset)[0]))


def write_tree(conn: sqlite3.Connection, preset: Preset) -> str:
    tree, blocks, extra = tree_object(preset)
    for bd in blocks:
        _put(conn, "blobs", bd)
    _put(conn, "blobs", extra)
    return _put(conn, "trees", tree)


def read_tree(conn: sqlite3.Connection, tree_id: str, collapsed_from: Preset | None = None) -> Preset:
    tree = _get(conn, "trees", tree_id)
    previous = {b.id: b.collapsed for b in collapsed_from.blocks} if collapsed_from else {}
    blocks = []
    for item in tree["blocks"]:
        data = _get(conn, "blobs", item["blob"])
        b = Block.model_validate({**data, "enabled": item["enabled"]})
        # 收合狀態只是 UI：沿用目前工作區；新出現的長區塊預設收合
        b.collapsed = previous.get(b.id, b.kind == "content" and len(b.content) > 300)
        blocks.append(b)
    return Preset(
        name=tree["name"],
        blocks=blocks,
        params=Params.model_validate(tree["params"]),
        options=Options.model_validate(tree["options"]),
        extra=_get(conn, "blobs", tree["extra"]),
    )


# ---- commit / ref ----

@dataclass
class Commit:
    id: str
    tree_id: str
    parents: list[str]
    author: str
    message: str
    created_at: str
    meta: dict

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _commit(row) -> Commit:
    return Commit(row["id"], row["tree_id"], json.loads(row["parents"]), row["author"], row["message"],
                  row["created_at"], json.loads(row["meta"]))


def get_commit(conn: sqlite3.Connection, cid: str) -> Commit:
    rows = conn.execute("SELECT * FROM commits WHERE id LIKE ?", (cid + "%",)).fetchall()
    if not rows:
        raise VcsError(f"找不到 commit {cid[:8]}")
    if len(rows) > 1:
        raise VcsError(f"commit 前綴 {cid} 不唯一")
    return _commit(rows[0])


def create_commit(
    conn: sqlite3.Connection, preset: Preset, parents: list[str], message: str, author: str = "human", meta: dict | None = None
) -> Commit:
    if not message.strip():
        raise VcsError("提交說明不能是空的")
    with conn:
        tree_id = write_tree(conn, preset)
        body = {"tree": tree_id, "parents": parents, "author": author, "message": message.strip(),
                "created_at": now_iso(), "meta": meta or {}}
        cid = _hash(_canon(body))
        conn.execute(
            "INSERT OR IGNORE INTO commits (id, tree_id, parents, author, message, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, tree_id, json.dumps(parents), author, body["message"], _canon(body["meta"]), body["created_at"]),
        )
    return get_commit(conn, cid)


def list_refs(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) | {"protected": bool(r["protected"])} for r in conn.execute("SELECT * FROM refs ORDER BY kind, name")]


def get_ref(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM refs WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def set_ref(conn: sqlite3.Connection, name: str, commit_id: str, kind: str = "branch", protected: bool | None = None) -> None:
    name = name.strip()
    if not name or any(c in name for c in " ~^:?*[\\"):
        raise VcsError(f"名稱不合法：{name!r}")
    existing = get_ref(conn, name)
    if existing and existing["kind"] != kind:
        raise VcsError(f"「{name}」已經是{'分支' if existing['kind'] == 'branch' else '標籤'}")
    if existing and kind == "tag":
        raise VcsError(f"標籤「{name}」已存在")
    prot = existing["protected"] if existing and protected is None else int(bool(protected))
    with conn:
        conn.execute(
            """INSERT INTO refs (name, kind, commit_id, protected, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET commit_id = excluded.commit_id, protected = excluded.protected,
                                               updated_at = excluded.updated_at""",
            (name, kind, commit_id, prot, now_iso()),
        )


def delete_ref(conn: sqlite3.Connection, name: str, current_branch: str | None) -> None:
    ref = get_ref(conn, name)
    if ref is None:
        raise VcsError(f"找不到「{name}」")
    if ref["protected"]:
        raise VcsError(f"「{name}」是受保護的分支")
    if ref["kind"] == "branch" and name == current_branch:
        raise VcsError("不能刪除目前所在的分支")
    with conn:
        conn.execute("DELETE FROM refs WHERE name = ?", (name,))


def log(conn: sqlite3.Connection, limit: int = 500) -> list[Commit]:
    rows = conn.execute("SELECT * FROM commits ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [_commit(r) for r in rows]


def commit_for_tree(conn: sqlite3.Connection, tree_id: str) -> str | None:
    row = conn.execute("SELECT id FROM commits WHERE tree_id = ? ORDER BY created_at DESC LIMIT 1", (tree_id,)).fetchone()
    return row["id"] if row else None


# ---- diff ----

BLOCK_FIELDS = ["name", "kind", "marker", "role", "content", "injection_position", "injection_depth", "injection_order", "group"]


def _lcs(a: list[str], b: list[str]) -> set[str]:
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    keep, i, j = set(), 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            keep.add(a[i])
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return keep


def _dict_diff(a: dict, b: dict) -> dict:
    return {k: [a.get(k), b.get(k)] for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def diff_presets(a: Preset, b: Preset) -> dict:
    """區塊以 id 對齊：新增 / 刪除 / 修改（欄位）/ 開關 / 移動（不在順序 LCS 中）。"""
    a_blocks = {x.id: x for x in a.blocks}
    b_blocks = {x.id: x for x in b.blocks}
    common_a = [x.id for x in a.blocks if x.id in b_blocks]
    common_b = [x.id for x in b.blocks if x.id in a_blocks]
    stable = _lcs(common_a, common_b)

    entries = []
    for idx, nb in enumerate(b.blocks):
        ob = a_blocks.get(nb.id)
        if ob is None:
            entries.append({"id": nb.id, "name": nb.name, "status": ["added"], "changes": {}, "old_index": None,
                            "new_index": idx, "old": None, "new": nb.model_dump(mode="json")})
            continue
        changes = {f: [getattr(ob, f), getattr(nb, f)] for f in BLOCK_FIELDS if getattr(ob, f) != getattr(nb, f)}
        status = []
        if changes:
            status.append("modified")
        if ob.enabled != nb.enabled:
            status.append("toggled")
            changes["enabled"] = [ob.enabled, nb.enabled]
        if nb.id not in stable:
            status.append("moved")
        entries.append({"id": nb.id, "name": nb.name, "status": status or ["unchanged"], "changes": changes,
                        "old_index": a.blocks.index(ob), "new_index": idx,
                        "old": ob.model_dump(mode="json"), "new": nb.model_dump(mode="json")})

    # 刪除的區塊放回原位置附近：插在原本前一個仍存在的區塊之後
    for oidx, ob in enumerate(a.blocks):
        if ob.id in b_blocks:
            continue
        prev = next((a.blocks[k].id for k in range(oidx - 1, -1, -1) if a.blocks[k].id in b_blocks), None)
        pos = next((i + 1 for i, e in enumerate(entries) if e["id"] == prev), 0) if prev else 0
        while pos < len(entries) and "removed" in entries[pos]["status"]:
            pos += 1
        entries.insert(pos, {"id": ob.id, "name": ob.name, "status": ["removed"], "changes": {}, "old_index": oidx,
                             "new_index": None, "old": ob.model_dump(mode="json"), "new": None})

    summary = {s: sum(s in e["status"] for e in entries) for s in ("added", "removed", "modified", "toggled", "moved")}
    return {
        "name": [a.name, b.name] if a.name != b.name else None,
        "params": _dict_diff(a.params.model_dump(), b.params.model_dump()),
        "options": _dict_diff(a.options.model_dump(), b.options.model_dump()),
        "extra_changed": _canon(a.extra) != _canon(b.extra),
        "summary": summary,
        "blocks": entries,
    }
