"""腳本（design.md §3）：金字塔階段設定，以「父腳本 + 覆寫」構成繼承樹。

覆寫以路徑表示，例如 {"stages[0].seeds": 6}；根腳本相對於 ScriptData 的預設值覆寫。
編輯時前端送出完整數值，由 compute_overrides 算出與父腳本不同的最小覆寫，
因此父腳本的修改會自動傳到沒有覆寫該欄位的子腳本。
"""

import hashlib
import json
import re
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .store import now_iso

MAX_TURNS = 30
MAX_SEEDS = 50

PromoteMode = Literal["manual", "llm_assist", "top_k", "all", "restart"]
# 使用者發言：情境第一句 + LLM ／ 情境逐回合輸入優先、不足由 LLM 補 ／ 全部由 LLM 扮演
UserSource = Literal["scenario_first", "scenario_all", "llm"]


class Promote(BaseModel):
    mode: PromoteMode = "manual"
    k: int = Field(3, ge=1, le=MAX_SEEDS)


class Stage(BaseModel):
    name: str = ""
    # 累計回合數：本階段結束時對話的總回合數（restart 的下一階段從開場重算）
    turns: int = Field(1, ge=1, le=MAX_TURNS)
    # 每個起點（第一階段是每個情境，之後是每條晉級的分支）長出幾條分支
    seeds: int = Field(1, ge=1, le=MAX_SEEDS)
    # 溫度掃描：空 = 用預設的溫度；多個值時分支數 × 組數
    temperatures: list[float] = Field(default_factory=list)
    # 本階段結束後如何挑選晉級到下一階段的分支
    promote: Promote = Field(default_factory=Promote)


def _default_stages() -> list[Stage]:
    return [
        Stage(name="廣篩", turns=1, seeds=6, promote=Promote(mode="manual")),
        Stage(name="穩定度", turns=4, seeds=1, promote=Promote(mode="manual")),
    ]


class ScriptData(BaseModel):
    stages: list[Stage] = Field(default_factory=_default_stages, min_length=1, max_length=8)
    user_source: UserSource = "scenario_first"
    concurrency: int = Field(3, ge=1, le=16)


class Script(BaseModel):
    id: int | None = None
    name: str = "新腳本"
    parent_id: int | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    updated_at: str | None = None


# ---- 路徑 ----

_PART = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _parts(path: str) -> list[str | int]:
    return [int(i) if i else k for k, i in _PART.findall(path)]


def set_path(data: Any, path: str, value: Any) -> None:
    parts = _parts(path)
    cur = data
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value


def get_path(data: Any, path: str) -> Any:
    cur = data
    for p in _parts(path):
        cur = cur[p]
    return cur


def apply_overrides(base: dict, overrides: dict[str, Any]) -> tuple[dict, list[str]]:
    """套用覆寫；父腳本結構改變（例如階段變少）導致路徑不存在時略過並回報。"""
    data = json.loads(json.dumps(base))
    warnings = []
    # 先套整個欄位（較短的路徑），再套細項
    for path in sorted(overrides, key=lambda p: len(_parts(p))):
        try:
            set_path(data, path, overrides[path])
        except (KeyError, IndexError, TypeError):
            warnings.append(f"覆寫「{path}」對不到父腳本的欄位，已略過")
    return data, warnings


def compute_overrides(parent: dict, new: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in new.items():
        path = f"{prefix}.{key}" if prefix else key
        old = parent.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            out |= compute_overrides(old, value, path)
        elif (
            isinstance(value, list)
            and isinstance(old, list)
            and len(value) == len(old)
            and value
            and all(isinstance(v, dict) for v in value + old)
        ):
            for i, (o, v) in enumerate(zip(old, value)):
                out |= compute_overrides(o, v, f"{path}[{i}]")
        elif value != old:
            out[path] = value
    return out


# ---- 儲存 ----

def _row(row) -> Script:
    return Script(id=row["id"], name=row["name"], parent_id=row["parent_id"], overrides=json.loads(row["overrides"]),
                  notes=row["notes"], updated_at=row["updated_at"])


def get(conn: sqlite3.Connection, script_id: int) -> Script | None:
    row = conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    return _row(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Script]:
    return [_row(r) for r in conn.execute("SELECT * FROM scripts ORDER BY id").fetchall()]


def chain(conn: sqlite3.Connection, script_id: int) -> list[Script]:
    """由根到自己。"""
    out: list[Script] = []
    seen: set[int] = set()
    cur: int | None = script_id
    while cur is not None:
        if cur in seen:
            raise ValueError("腳本的父子關係形成循環")
        seen.add(cur)
        s = get(conn, cur)
        if s is None:
            raise KeyError(f"找不到腳本 #{cur}")
        out.append(s)
        cur = s.parent_id
    return out[::-1]


def resolve_raw(conn: sqlite3.Connection, script_id: int | None) -> tuple[dict, list[str]]:
    data = ScriptData().model_dump()
    warnings: list[str] = []
    if script_id is None:
        return data, warnings
    for s in chain(conn, script_id):
        data, w = apply_overrides(data, s.overrides)
        warnings += [f"{s.name}：{x}" for x in w]
    return data, warnings


def resolve(conn: sqlite3.Connection, script_id: int) -> tuple[ScriptData, list[str]]:
    raw, warnings = resolve_raw(conn, script_id)
    try:
        return ScriptData.model_validate(raw), warnings
    except ValidationError as e:
        raise ValueError(f"腳本解析後的數值不正確：{e.errors()[0]['msg']}") from e


def snapshot_hash(data: ScriptData) -> str:
    return hashlib.sha256(data.model_dump_json().encode()).hexdigest()


def save(conn: sqlite3.Connection, script: Script, data: ScriptData | None = None) -> Script:
    """data 有值時，依父腳本解析結果重新計算覆寫。"""
    if script.parent_id is not None:
        if script.id is not None and script.id in {s.id for s in chain(conn, script.parent_id)}:
            raise ValueError("不能把腳本接到自己的子孫底下")
    if data is not None:
        parent, _ = resolve_raw(conn, script.parent_id)
        script.overrides = compute_overrides(parent, data.model_dump())
    script.updated_at = now_iso()
    with conn:
        if script.id is None:
            script.id = conn.execute(
                "INSERT INTO scripts (name, parent_id, overrides, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
                (script.name, script.parent_id, json.dumps(script.overrides, ensure_ascii=False), script.notes,
                 script.updated_at),
            ).lastrowid
        else:
            conn.execute(
                "UPDATE scripts SET name = ?, parent_id = ?, overrides = ?, notes = ?, updated_at = ? WHERE id = ?",
                (script.name, script.parent_id, json.dumps(script.overrides, ensure_ascii=False), script.notes,
                 script.updated_at, script.id),
            )
    return script


def delete(conn: sqlite3.Connection, script_id: int) -> None:
    if conn.execute("SELECT 1 FROM scripts WHERE parent_id = ?", (script_id,)).fetchone():
        raise ValueError("這個腳本還有子腳本，請先刪除或改接子腳本")
    with conn:
        conn.execute("DELETE FROM scripts WHERE id = ?", (script_id,))


def ensure_starter(conn: sqlite3.Connection) -> None:
    """沒有任何腳本時建立「金字塔-標準」與子腳本「金字塔-快速」（只覆寫第一階段 seed 數）。"""
    if conn.execute("SELECT 1 FROM scripts").fetchone():
        return
    standard = ScriptData(
        stages=[
            Stage(name="廣篩", turns=1, seeds=8, promote=Promote(mode="top_k", k=3)),
            Stage(name="穩定度", turns=4, seeds=1, promote=Promote(mode="manual")),
            Stage(name="長回合", turns=10, seeds=1),
        ]
    )
    root = save(conn, Script(name="金字塔-標準", notes="單回合多 seed 篩選 → 多回合 → 長回合"), standard)
    quick = standard.model_copy(deep=True)
    quick.stages[0].seeds = 4
    save(conn, Script(name="金字塔-快速", parent_id=root.id, notes="只覆寫第一階段 seed 數"), quick)
