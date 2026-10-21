"""人工評語（design.md §7.1）：自由輸入，任何「名稱：數字/10」的行都會解析成分數。"""

import json
import re
import sqlite3
from statistics import mean

from .store import now_iso

TARGET_TYPES = ("run", "run_message", "generation")

# 只是佔位文字，使用者可在設定頁改寫；屬性名稱不固定
DEFAULT_TEMPLATE = """【評分】
- 屬性：_/10
【問題】
- （引用原文片段）→ 問題描述
【修改方向】
- """

_SCORE = re.compile(
    r"^[ \t]*(?:[-*•·][ \t]*)?(?P<name>[^\n:：/]{1,30}?)[ \t]*[:：][ \t]*(?P<score>\d+(?:\.\d+)?)[ \t]*/[ \t]*10(?!\d)",
    re.M,
)


def parse_scores(text: str) -> dict[str, float]:
    """「文風：7/10」「- 格式完整: 8.5 / 10」→ {"文風": 7, "格式完整": 8.5}；佔位的「_/10」與超出範圍的值略過。"""
    scores: dict[str, float] = {}
    for m in _SCORE.finditer(text):
        value = float(m.group("score"))
        name = m.group("name").strip().strip("【】[]「」")
        if name and 0 <= value <= 10:
            scores[name] = int(value) if value.is_integer() else value
    return scores


def _row(row) -> dict:
    d = dict(row)
    d["scores"] = json.loads(d["scores"])
    return d


def _run_id_for(conn: sqlite3.Connection, target_type: str, target_id: int) -> int | None:
    if target_type == "run":
        return target_id
    if target_type == "run_message":
        row = conn.execute("SELECT run_id FROM run_messages WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise KeyError(f"找不到訊息 #{target_id}")
        return row["run_id"]
    return None


def add(conn: sqlite3.Connection, target_type: str, target_id: int, text: str) -> dict:
    if target_type not in TARGET_TYPES:
        raise ValueError(f"target_type 必須是 {'、'.join(TARGET_TYPES)}")
    run_id = _run_id_for(conn, target_type, target_id)
    ts = now_iso()
    with conn:
        fid = conn.execute(
            """INSERT INTO feedback (target_type, target_id, run_id, text, scores, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (target_type, target_id, run_id, text, json.dumps(parse_scores(text), ensure_ascii=False), ts, ts),
        ).lastrowid
    return get(conn, fid)


def update(conn: sqlite3.Connection, fid: int, text: str) -> dict:
    with conn:
        n = conn.execute(
            "UPDATE feedback SET text = ?, scores = ?, updated_at = ? WHERE id = ?",
            (text, json.dumps(parse_scores(text), ensure_ascii=False), now_iso(), fid),
        ).rowcount
    if not n:
        raise KeyError(f"找不到評語 #{fid}")
    return get(conn, fid)


def get(conn: sqlite3.Connection, fid: int) -> dict:
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (fid,)).fetchone()
    if row is None:
        raise KeyError(f"找不到評語 #{fid}")
    return _row(row)


def delete(conn: sqlite3.Connection, fid: int) -> None:
    with conn:
        conn.execute("DELETE FROM feedback WHERE id = ?", (fid,))


def list_for(conn: sqlite3.Connection, *, target_type: str | None = None, target_id: int | None = None,
             run_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM feedback WHERE 1 = 1", []
    if target_type:
        sql += " AND target_type = ?"
        args.append(target_type)
    if target_id is not None:
        sql += " AND target_id = ?"
        args.append(target_id)
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    return [_row(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]


def aggregate(feedbacks: list[dict]) -> dict:
    """各屬性平均與整體平均（所有分數值的平均）。"""
    per: dict[str, list[float]] = {}
    for f in feedbacks:
        for name, value in f["scores"].items():
            per.setdefault(name, []).append(value)
    all_values = [v for vs in per.values() for v in vs]
    return {
        "attributes": {k: round(mean(v), 2) for k, v in per.items()},
        "counts": {k: len(v) for k, v in per.items()},
        "overall": round(mean(all_values), 2) if all_values else None,
        "feedback_count": len(feedbacks),
    }


def run_scores(conn: sqlite3.Connection, run_id: int) -> dict:
    return aggregate(list_for(conn, run_id=run_id))


def overall_by_run(conn: sqlite3.Connection) -> dict[int, float]:
    out: dict[int, list[float]] = {}
    for r in conn.execute("SELECT run_id, scores FROM feedback WHERE run_id IS NOT NULL"):
        out.setdefault(r["run_id"], []).extend(json.loads(r["scores"]).values())
    return {k: round(mean(v), 2) for k, v in out.items() if v}


def overall_by_commit(conn: sqlite3.Connection) -> dict[str, dict]:
    """commit → {overall, scored}：該版本所有試跑與生成的評語分數平均。"""
    values: dict[str, list[float]] = {}
    rows = conn.execute(
        """SELECT r.commit_id AS cid, f.scores FROM feedback f JOIN runs r ON r.id = f.run_id
           WHERE r.commit_id IS NOT NULL
           UNION ALL
           SELECT g.commit_id AS cid, f.scores FROM feedback f JOIN generations g ON g.id = f.target_id
           WHERE f.target_type = 'generation' AND g.commit_id IS NOT NULL"""
    )
    for r in rows:
        values.setdefault(r["cid"], []).extend(json.loads(r["scores"]).values())
    return {k: {"overall": round(mean(v), 2), "scored": len(v)} for k, v in values.items() if v}
