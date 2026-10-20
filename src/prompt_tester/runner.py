"""LLM 呼叫的共用部分，以及多回合試跑（LLM 扮演 {{user}} 或回放舊紀錄）。

多回合試跑每回合：
1. 使用者發言：回放舊紀錄（replay）／情境的第一句（只用於第 1 回合）／模擬使用者 LLM（live）
2. 依預設組裝 prompt（含角色卡、世界書），角色模型回覆
兩邊都串流回前端；每則訊息記錄來源、用量與費用。
"""

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel

from . import llm, providers, store
from .build import build
from .entities import Connection, ReasoningEffort, Scenario
from .macros import MacroContext, evaluate
from .preset import HistoryMessage, Params, Preset

# ---- 共用：request body、費用、SSE ----


def request_body(
    messages: list[dict],
    params: Params,
    connection: Connection,
    seed: int | None,
    provider: providers.Provider,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    body: dict = {
        "model": connection.model,
        "messages": messages,
        "temperature": params.temperature if temperature is None else temperature,
        "top_p": params.top_p,
        "frequency_penalty": params.frequency_penalty,
        "presence_penalty": params.presence_penalty,
        "max_tokens": params.max_tokens if max_tokens is None else max_tokens,
    }
    if connection.send_seed and seed is not None:
        body["seed"] = seed
    if provider.is_openrouter:
        if routing := connection.routing.to_body():
            body["provider"] = routing
        if connection.reasoning_effort:
            body["reasoning"] = {"effort": connection.reasoning_effort}
    elif connection.reasoning_effort:
        body["reasoning_effort"] = connection.reasoning_effort
    body.update(connection.extra_body)
    return body


def estimate_cost(provider: providers.Provider, model: str, usage: dict | None) -> float | None:
    info = providers.cached_model(provider, model)
    if not info or not usage:
        return None
    p = info["pricing"]
    if p["prompt"] is None or p["completion"] is None:
        return None
    return (usage.get("prompt_tokens", 0) * p["prompt"] + usage.get("completion_tokens", 0) * p["completion"]) / 1e6


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@dataclass
class CallResult:
    output: str = ""
    reasoning: str = ""
    usage: dict | None = None
    finish_reason: str | None = None
    served_by: str | None = None
    model: str | None = None
    remote_id: str | None = None
    first_token_ms: int | None = None
    latency_ms: int = 0
    cost: float | None = None
    cost_source: str | None = None
    error: str | None = None


async def stream_call(
    http: httpx.AsyncClient,
    provider: providers.Provider,
    body: dict,
    result: CallResult,
    *,
    lookup_stats: bool = True,
) -> AsyncIterator[dict]:
    """串流一次呼叫，逐一產生 content / reasoning / meta 事件，並把結果填入 result（錯誤不丟出，寫進 result.error）。"""
    t0 = llm.now_ms()
    out: list[str] = []
    reasoning: list[str] = []
    try:
        async for ev in llm.stream_chat(http, provider, body):
            kind = ev["type"]
            if kind in ("content", "reasoning"):
                if result.first_token_ms is None:
                    result.first_token_ms = llm.now_ms() - t0
                (out if kind == "content" else reasoning).append(ev["text"])
                yield ev
            elif kind == "meta":
                result.served_by = ev.get("provider")
                result.model = ev.get("model")
                result.remote_id = ev.get("id")
                yield ev
            elif kind == "finish":
                result.finish_reason = ev["reason"]
            elif kind == "usage":
                result.usage = ev["usage"]
    except llm.LLMError as e:
        result.error = str(e)
    finally:
        result.output = "".join(out)
        result.reasoning = "".join(reasoning)
        result.latency_ms = llm.now_ms() - t0
    if result.error:
        return

    result.cost = (result.usage or {}).get("cost")
    result.cost_source = "api" if result.cost is not None else None
    if lookup_stats and (result.cost is None or not result.served_by):
        stats = await llm.fetch_generation_stats(http, provider, result.remote_id or "")
        if stats:
            result.served_by = result.served_by or stats.get("provider_name")
            if result.cost is None and stats.get("total_cost") is not None:
                result.cost, result.cost_source = stats["total_cost"], "generation"
    if result.cost is None:
        result.cost = estimate_cost(provider, body.get("model", ""), result.usage)
        result.cost_source = "estimate" if result.cost is not None else None


# ---- 模擬使用者 ----

DEFAULT_USER_TEMPLATE = """你現在扮演一段角色扮演對話中的 {{user}}，正在與 {{char}} 互動。

【{{user}} 的人設】
{{persona}}

【{{user}} 在這段劇情中的目標】
{{user_goal}}

規則：
- 只寫 {{user}} 接下來要說的話，可以用括號附上簡短動作；1～3 句，使用繁體中文。
- 不描寫 {{char}} 的反應、想法或台詞；不寫旁白、狀態欄或任何格式化內容。
- 回應 {{char}} 最新的發言，讓劇情自然地朝目標推進；不要重複先前說過的話。
- 保持在角色中，不評論故事、不提到 AI 或系統。"""


class UserSimSettings(BaseModel):
    """模擬使用者的全域設定（存在 kv 表）。"""

    connection_id: int | None = None
    template: str = DEFAULT_USER_TEMPLATE
    temperature: float = 0.9
    # 推理型模型的推理 token 也計入上限，留足空間
    max_tokens: int = 1500
    # 只給模擬使用者看最近幾則訊息
    context_messages: int = 8
    # 剝除角色回覆中的思考鏈、狀態欄 HTML、style 等格式，只留劇情文字
    strip_markup: bool = True
    reasoning_effort: ReasoningEffort | None = None


_BLOCKS = re.compile(r"<(details|style|think|thinking|script)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_CODE = re.compile(r"```.*?```", re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>")
MAX_TRANSCRIPT_CHARS = 2000


def strip_markup(text: str) -> str:
    text = _COMMENT.sub("", _CODE.sub("", _BLOCKS.sub("", text)))
    text = _TAG.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TRANSCRIPT_CHARS] + ("…" if len(text) > MAX_TRANSCRIPT_CHARS else "")


def user_sim_messages(
    settings: UserSimSettings, scenario: Scenario, history: list[HistoryMessage], names: dict[str, str]
) -> list[dict]:
    ctx = MacroContext(names={**names, "persona": scenario.persona or "（未設定）", "user_goal": scenario.user_goal or "（自由發揮）"})
    system = evaluate(settings.template, ctx)
    char, user = names.get("char", "角色"), names.get("user", "使用者")
    lines = []
    for h in history[-settings.context_messages :]:
        speaker = user if h.role == "user" else char if h.role == "assistant" else "旁白"
        content = evaluate(h.content, MacroContext(names=names))
        lines.append(f"{speaker}：{strip_markup(content) if settings.strip_markup else content}")
    transcript = "\n\n".join(lines) if lines else "（對話尚未開始）"
    prompt = f"以下是目前的對話紀錄（由舊到新）：\n\n{transcript}\n\n請寫出 {user} 的下一句發言。只輸出 {user} 的發言本身。"
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]


def clean_user_output(text: str, user_name: str) -> str:
    text = text.strip()
    for prefix in (f"{user_name}：", f"{user_name}:", "{{user}}：", "{{user}}:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text


# ---- 預檢的啟發式檢查 ----

REFUSAL = re.compile(
    r"無法協助|不能協助|不能在這個框架|我不會(?:繼續|參與)|需要先暫停|作為(?:一個)?\s*AI|身為(?:一個)?\s*AI|"
    r"違反.{0,8}(?:政策|規範|準則)|as an ai|i can(?:'|’)?t (?:help|continue|assist)|i cannot (?:help|continue|assist)",
    re.I,
)


def check_message(role: str, output: str, finish: str | None, error: str | None) -> list[str]:
    issues = []
    if error:
        issues.append(f"呼叫失敗：{error}")
        return issues
    if not output.strip():
        issues.append("沒有產生內容" + ("（推理用完 max_tokens）" if finish == "length" else ""))
    elif finish == "length":
        issues.append("被 max_tokens 截斷")
    if role == "assistant" and REFUSAL.search(output[:600]):
        issues.append("可能出戲或拒答（啟發式比對，請人工確認）")
    if role == "assistant" and 0 < len(output.strip()) < 20:
        issues.append("回覆過短")
    return issues


# ---- 多回合試跑 ----

@dataclass
class RunSpec:
    preset: Preset
    scenario: Scenario
    connection: Connection
    provider: providers.Provider
    turns: int
    seed: int
    mode: str  # live | replay
    replay_user_turns: list[str] = field(default_factory=list)
    user_connection: Connection | None = None
    user_provider: providers.Provider | None = None
    user_settings: UserSimSettings = field(default_factory=UserSimSettings)
    # 第 1 回合優先使用情境的第一句使用者輸入
    first_from_scenario: bool = True


async def run_session(
    conn: sqlite3.Connection, http: httpx.AsyncClient, spec: RunSpec, run_id: int, *, lookup_stats: bool = True
) -> AsyncIterator[str]:
    sc = spec.scenario
    names = sc.names()
    history: list[HistoryMessage] = sc.chat()
    first_line = next((t for t in sc.user_turns if t.strip()), "")
    total_cost = 0.0
    idx = 0
    status, run_error = "done", None
    build_warnings: list[str] = []
    issues: list[dict] = []

    def save(turn: int, role: str, source: str, r: CallResult | None, content: str, request: dict | None) -> dict:
        nonlocal idx, total_cost
        idx += 1
        msg = store.add_run_message(
            conn,
            run_id,
            idx=idx,
            turn=turn,
            role=role,
            source=source,
            content=content,
            result=r,
            request=request,
        )
        total_cost += (r.cost or 0) if r else 0
        return msg

    yield sse({"type": "start", "run_id": run_id, "turns": spec.turns, "history": [h.model_dump() for h in history]})
    try:
        for turn in range(1, spec.turns + 1):
            # 1. 使用者發言
            yield sse({"type": "message_start", "turn": turn, "role": "user"})
            if spec.mode == "replay" and turn <= len(spec.replay_user_turns):
                text, source, r, req = spec.replay_user_turns[turn - 1], "replay", None, None
            elif turn == 1 and spec.first_from_scenario and first_line:
                text, source, r, req = first_line, "scenario", None, None
            else:
                if spec.user_connection is None or spec.user_provider is None:
                    raise RuntimeError("沒有設定模擬使用者的連線（設定頁 → 模擬使用者）")
                ucon = spec.user_connection
                if spec.user_settings.reasoning_effort is not None:
                    ucon = ucon.model_copy(update={"reasoning_effort": spec.user_settings.reasoning_effort})
                req = request_body(
                    user_sim_messages(spec.user_settings, sc, history, names),
                    spec.preset.params,
                    ucon,
                    spec.seed + turn,
                    spec.user_provider,
                    temperature=spec.user_settings.temperature,
                    max_tokens=spec.user_settings.max_tokens,
                )
                r = CallResult()
                async for ev in stream_call(http, spec.user_provider, req, r, lookup_stats=lookup_stats):
                    yield sse(ev)
                text = clean_user_output(r.output, names.get("user", ""))
                source = "replay_fallback" if spec.mode == "replay" else "llm_user"
            msg = save(turn, "user", source, r, text, req)
            for issue in check_message("user", text, r.finish_reason if r else None, r.error if r else None):
                issues.append({"turn": turn, "role": "user", "message": issue})
            yield sse({"type": "message_done", "message": msg})
            if r and r.error:
                status, run_error = "error", r.error
                break
            history.append(HistoryMessage(role="user", content=text))

            # 2. 角色回覆
            yield sse({"type": "message_start", "turn": turn, "role": "assistant"})
            built = build(spec.preset, history, names, seed=spec.seed + turn, card=sc.card, persona=sc.persona)
            if turn == 1:
                build_warnings = built.warnings
            req = request_body(built.to_api(), spec.preset.params, spec.connection, spec.seed + turn, spec.provider)
            r = CallResult()
            async for ev in stream_call(http, spec.provider, req, r, lookup_stats=lookup_stats):
                yield sse(ev)
            msg = save(turn, "assistant", "char", r, r.output, req)
            for issue in check_message("assistant", r.output, r.finish_reason, r.error):
                issues.append({"turn": turn, "role": "assistant", "message": issue})
            yield sse({"type": "message_done", "message": msg})
            if r.error:
                status, run_error = "error", r.error
                break
            history.append(HistoryMessage(role="assistant", content=r.output))
    except asyncio.CancelledError:
        store.finish_run(conn, run_id, status="cancelled", error="使用者中止", cost=total_cost,
                         checks={"build_warnings": build_warnings, "issues": issues})
        raise
    except RuntimeError as e:
        status, run_error = "error", str(e)

    checks = {"build_warnings": build_warnings, "issues": issues}
    store.finish_run(conn, run_id, status=status, error=run_error, cost=total_cost, checks=checks)
    yield sse({"type": "done", "run": store.get_run(conn, run_id)})
