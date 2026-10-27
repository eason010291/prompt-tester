"""情境（角色卡）的線性修訂紀錄（design.md §7.5，方案 B）。

- 每個情境一條歷史：父 → 子，不分支；需要平行版本時用「複製情境」。
- 修訂 = 情境內容的完整快照（不含 id / updated_at），以內容 hash 判斷是否有未存成版本的修改。
- 人工「存成版本」、LLM 修改角色卡後提交、建立實驗時（內容有變）各新增一筆。
- 欄位分兩類：作品（角色卡內容）與測試條件（歷史、使用者輸入、人設、目標…）；LLM 都能改（測試條件可在設定關閉），
  每次修改都存成新版本，可 diff、可還原。
"""

import hashlib
import json
import sqlite3

from . import store
from .entities import LoreEntry, Scenario

# (路徑, 標籤, 類別)；類別 card = 作品，fixture = 測試條件，meta = 名稱等；世界書條目另外比對
FIELDS: list[tuple[str, str, str]] = [
    ("card.name", "角色名稱", "card"),
    ("card.description", "描述", "card"),
    ("card.personality", "個性", "card"),
    ("card.scenario", "卡片情境", "card"),
    ("card.first_mes", "卡片開場白（first_mes）", "card"),
    ("card.alternate_greetings", "替代開場白", "card"),
    ("card.mes_example", "範例對話", "card"),
    ("card.system_prompt", "System prompt", "card"),
    ("card.post_history_instructions", "Post-history", "card"),
    ("card.depth_prompt.prompt", "深度提示", "card"),
    ("card.depth_prompt.depth", "深度提示深度", "card"),
    ("card.depth_prompt.role", "深度提示角色", "card"),
    ("card.creator_notes", "作者備註", "card"),
    ("card.tags", "標籤", "card"),
    ("card.creator", "作者", "card"),
    ("card.character_version", "角色版本", "card"),
    ("card.regex_scripts", "角色正則", "card"),
    ("card.book.name", "世界書名稱", "card"),
    ("card.book.scan_depth", "世界書掃描深度", "card"),
    ("card.book.token_budget", "世界書 token 預算", "card"),
    ("card.book.recursive_scanning", "世界書遞迴掃描", "card"),
    ("opening", "開場白", "card"),
    ("persona", "使用者人設", "fixture"),
    ("user_goal", "使用者目標", "fixture"),
    ("user_turns", "逐回合使用者輸入", "fixture"),
    ("history", "既有歷史", "fixture"),
    ("vars", "變數", "fixture"),
    ("char", "{{char}}", "meta"),
    ("user", "{{user}}", "meta"),
    ("name", "情境名稱", "meta"),
    ("notes", "備註", "meta"),
]

# 測試條件的頂層欄位（LLM 修改受「允許修改測試條件」設定控制）
FIXTURE_FIELDS = {path for path, _l, group in FIELDS if group == "fixture"}


def _content(sc: Scenario) -> dict:
    return sc.model_dump(mode="json", exclude={"id", "updated_at"})


def content_hash(sc: Scenario) -> str:
    return hashlib.sha256(json.dumps(_content(sc), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def get_path(sc: Scenario, path: str):
    cur = sc
    for part in path.split("."):
        if cur is None:
            return None
        cur = getattr(cur, part)
    return cur


def _row(r, with_data: bool = False) -> dict:
    d = {k: r[k] for k in ("id", "scenario_id", "created_at", "author", "message", "hash", "parent_id")}
    d["meta"] = json.loads(r["meta"])
    if with_data:
        d["data"] = json.loads(r["data"])
    return d


def latest(conn: sqlite3.Connection, sid: int) -> dict | None:
    r = conn.execute("SELECT * FROM scenario_revisions WHERE scenario_id = ? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    return _row(r) if r else None


def list_revisions(conn: sqlite3.Connection, sid: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM scenario_revisions WHERE scenario_id = ? ORDER BY id DESC", (sid,)).fetchall()
    out = [_row(r) for r in rows]
    for i, r in enumerate(out):
        r["number"] = len(out) - i
    return out


def get(conn: sqlite3.Connection, rid: int) -> dict | None:
    r = conn.execute("SELECT * FROM scenario_revisions WHERE id = ?", (rid,)).fetchone()
    return _row(r, with_data=True) if r else None


def scenario_of(rev: dict, sid: int) -> Scenario:
    sc = Scenario.model_validate(rev["data"])
    sc.id = sid
    return sc


def is_dirty(conn: sqlite3.Connection, sc: Scenario) -> bool:
    last = latest(conn, sc.id)
    return last is None or last["hash"] != content_hash(sc)


def create(conn: sqlite3.Connection, sc: Scenario, *, author: str = "human", message: str = "", meta: dict | None = None) -> tuple[dict, bool]:
    """存成版本；內容和最新版相同時不新增，回傳 (最新版, False)。"""
    h = content_hash(sc)
    last = latest(conn, sc.id)
    if last is not None and last["hash"] == h:
        return last, False
    with conn:
        rid = conn.execute(
            """INSERT INTO scenario_revisions (scenario_id, created_at, author, message, hash, data, parent_id, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sc.id, store.now_iso(), author, message.strip() or "（未填說明）", h, json.dumps(_content(sc), ensure_ascii=False),
             last["id"] if last else None, json.dumps(meta or {}, ensure_ascii=False)),
        ).lastrowid
    return get(conn, rid), True


def ensure(conn: sqlite3.Connection, sc: Scenario, *, author: str = "system", message: str) -> dict:
    """目前內容若還沒存成版本就自動存一版，回傳對應的修訂。"""
    return create(conn, sc, author=author, message=message)[0]


def restore(conn: sqlite3.Connection, sid: int, rid: int) -> Scenario:
    """情境內容換成這個版本（工作內容；要保留請再存成版本）。"""
    rev = get(conn, rid)
    if rev is None or rev["scenario_id"] != sid:
        raise KeyError(f"找不到版本 #{rid}")
    return store.save_scenario(conn, scenario_of(rev, sid))


def _lore(sc: Scenario) -> dict[int, LoreEntry]:
    book = sc.card.book if sc.card else None
    return {e.uid: e for e in (book.entries if book else [])}


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(x if isinstance(x, str) else f"{x.role}：{x.content}" if hasattr(x, "role")
                         else json.dumps(x, ensure_ascii=False) for x in v)
    if isinstance(v, dict):
        return "\n".join(f"{k} = {val}" for k, val in v.items())
    return str(v)


def diff(old: Scenario, new: Scenario) -> dict:
    """欄位與世界書條目層級的差異（文字逐字差異由前端標示）。"""
    fields = []
    for path, label, group in FIELDS:
        a, b = _text(get_path(old, path)), _text(get_path(new, path))
        if a != b:
            fields.append({"path": path, "label": label, "group": group, "old": a, "new": b})
    lo, ln = _lore(old), _lore(new)
    lore = []
    for uid in sorted(set(lo) | set(ln)):
        a, b = lo.get(uid), ln.get(uid)
        if a is None:
            lore.append({"uid": uid, "comment": b.comment, "status": "added", "changes": [], "old": "", "new": b.content})
        elif b is None:
            lore.append({"uid": uid, "comment": a.comment, "status": "removed", "changes": [], "old": a.content, "new": ""})
        else:
            da, db = a.model_dump(), b.model_dump()
            changes = [k for k in da if da[k] != db[k]]
            if changes:
                lore.append({"uid": uid, "comment": b.comment or a.comment, "status": "modified", "changes": changes,
                             "old": a.content, "new": b.content,
                             "old_keys": a.keys, "new_keys": b.keys, "old_enabled": a.enabled, "new_enabled": b.enabled})
    card_changed = any(f["group"] == "card" for f in fields) or bool(lore)
    return {"fields": fields, "lore": lore, "card_changed": card_changed,
            "fixture_changed": any(f["group"] == "fixture" for f in fields)}


def with_current_card(snapshot: Scenario, current: Scenario | None) -> Scenario:
    """把快照（含既有對話）的角色卡換成目前的版本：迴歸試算與驗證輪次用，歷史本身不動。"""
    if current is None or current.card is None:
        return snapshot
    return snapshot.model_copy(update={"card": current.card.model_copy(deep=True)})
