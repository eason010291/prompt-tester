"""自動指標（design.md §7.4，2026-10-22 先做簡版）。

目前只有一條內建規則：「不是…而是…」句型。保留擴充空間：指標是一份清單（kv：metrics），
每項是正則 + 計數範圍 + 方向，之後可加自訂規則、重複（n-gram）、格式完整度等類型。
不用 LLM，讀取時即時計算，因此修改規則後舊資料也會用新規則重算。
"""

import re
import sqlite3
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from . import store
from .runner import strip_markup

METRICS_KEY = "metrics"


class Metric(BaseModel):
    id: str
    name: str
    kind: Literal["regex"] = "regex"
    pattern: str
    # body：只數正文（去掉思考鏈、狀態欄等 HTML / 程式碼區塊）；all：全文
    scope: Literal["body", "all"] = "body"
    # lower：越少越好（目前只用到這個方向）
    direction: Literal["lower", "higher"] = "lower"
    # 每則回覆超過這個次數就標示警告；None 不警告
    warn_at: int | None = 2
    enabled: bool = True


DEFAULT_METRICS = [
    Metric(
        id="not_but",
        name="不是…而是…",
        # 同一句內（不跨句號、問號、驚嘆號、換行）
        pattern=r"(?:並不是|不是|並非)[^。！？!?\n]{0,40}?而是",
    ),
]


class MetricSettings(BaseModel):
    metrics: list[Metric] = Field(default_factory=lambda: [m.model_copy() for m in DEFAULT_METRICS])


def load(conn: sqlite3.Connection) -> list[Metric]:
    return [m for m in MetricSettings.model_validate(store.get_setting(conn, METRICS_KEY) or {}).metrics if m.enabled]


@lru_cache(maxsize=64)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def measure(text: str, metrics: list[Metric]) -> dict[str, int]:
    """每個指標在這段文字中出現的次數。"""
    body = None
    out = {}
    for m in metrics:
        if m.scope == "body":
            if body is None:
                body = strip_markup(text, limit=None)
            target = body
        else:
            target = text
        out[m.id] = len(_compile(m.pattern).findall(target))
    return out


def run_metrics(messages: list[dict], metrics: list[Metric]) -> dict:
    """一條分支：每則角色回覆的次數，以及合計與「每則平均」。"""
    per_msg = {m["id"]: measure(m["content"], metrics) for m in messages if m["role"] == "assistant" and not m.get("error")}
    replies = len(per_msg)
    totals = {k.id: sum(v[k.id] for v in per_msg.values()) for k in metrics}
    return {
        "messages": per_msg,
        "totals": totals,
        "per_reply": {k: round(v / replies, 2) if replies else None for k, v in totals.items()},
        "replies": replies,
    }


def describe(metrics: list[Metric]) -> list[dict]:
    return [m.model_dump(include={"id", "name", "direction", "warn_at"}) for m in metrics]
