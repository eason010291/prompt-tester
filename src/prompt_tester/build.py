"""把預設 + 情境組成 Chat Completion messages[]。

組裝順序依 SillyTavern 1.18.0（public/scripts/openai.js），詳見 doc/design.md §5.2：
1. 依列表順序展開啟用的內容區塊巨集（相對與深度注入都在這一步展開）
2. 深度注入插入聊天歷史（populationInjectionPrompts）
3. 從最新往最舊放入歷史，超出預算即停止；最前面加 new_chat_prompt（populateChatHistory）
4. 依列表順序排出最終訊息；沒有啟用 chatHistory marker 就完全不送歷史與深度注入
5. squash_system_messages、去掉空訊息（getChat）
6. 後處理（src/prompt-converters.js postProcessPrompt）
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from . import tokens
from .macros import MacroContext, evaluate
from .preset import POSITION_IN_CHAT, Block, HistoryMessage, Preset

MAX_INJECTION_DEPTH = 10000
ROLE_PRIORITY = ("system", "user", "assistant")

Origin = Literal["block", "history", "injection", "separator", "placeholder"]


@dataclass
class Source:
    origin: Origin
    block_id: str | None = None
    name: str = ""


@dataclass
class Msg:
    role: str
    content: str
    sources: list[Source]
    name: str | None = None
    # ST 中不參與 squash 的特殊訊息（newMainChat 等）
    squashable: bool = True

    @property
    def tokens(self) -> int:
        return tokens.count_message(self.content)


@dataclass
class BuildResult:
    messages: list[Msg]
    budget: int
    history_total: int
    history_included: int
    variables: dict[str, str]
    # 每個啟用區塊展開巨集後的 token 數（不論是否被 squash 合併或因預算未送出）
    block_tokens: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(m.tokens for m in self.messages) + tokens.REPLY_PRIMING

    def to_api(self) -> list[dict]:
        out = []
        for m in self.messages:
            d = {"role": m.role, "content": m.content}
            if m.name:
                d["name"] = m.name
            out.append(d)
        return out


def build(
    preset: Preset,
    history: list[HistoryMessage],
    names: dict[str, str],
    *,
    seed: int = 0,
    now: datetime | None = None,
    variables: dict[str, str] | None = None,
    expand_macros: bool = True,
) -> BuildResult:
    ctx = MacroContext(
        names=names,
        variables=dict(variables or {}),
        seed=seed,
        now=now or datetime.now(),
        last_message=history[-1].content if history else "",
        last_user_message=next((h.content for h in reversed(history) if h.role == "user"), ""),
        last_char_message=next((h.content for h in reversed(history) if h.role == "assistant"), ""),
        max_context=preset.params.max_context,
        max_response=preset.params.max_tokens,
    )
    expand = (lambda text: evaluate(text, ctx)) if expand_macros else (lambda text: text)

    # 1. 展開區塊（ST getPromptCollection：依 prompt_order 順序，每個區塊一次）
    prepared: dict[str, str] = {}
    for b in preset.blocks:
        if b.kind == "content" and b.enabled:
            prepared[b.id] = expand(b.content)

    history_marker = next(
        (b for b in preset.blocks if b.kind == "marker" and b.marker == "chatHistory" and b.enabled),
        None,
    )

    budget = preset.params.max_context - preset.params.max_tokens - tokens.REPLY_PRIMING
    relative = [
        Msg(b.role, prepared[b.id], [Source("block", b.id, b.name)])
        for b in preset.blocks
        if b.id in prepared and b.injection_position != POSITION_IN_CHAT
    ]
    used = sum(m.tokens for m in relative)

    chat_msgs: list[Msg] = []
    history_included = 0
    if history_marker is not None:
        absolute = [b for b in preset.blocks if b.id in prepared and b.injection_position == POSITION_IN_CHAT]
        pool = _inject(absolute, prepared, history)

        separator = Msg("system", expand(preset.options.new_chat_prompt), [Source("separator", name="new_chat_prompt")], squashable=False)
        remaining = budget - used - separator.tokens
        # 3. 由新到舊，每則訊息展開巨集後放入，放不下即停止
        for item in reversed(pool):
            msg = Msg(item.role, expand(item.content), item.sources, item.name)
            if msg.tokens > remaining:
                break
            remaining -= msg.tokens
            chat_msgs.insert(0, msg)
            if msg.sources[0].origin == "history":
                history_included += 1
        chat_msgs.insert(0, separator)

    # 4. 依列表順序排出
    messages: list[Msg] = []
    rel_iter = iter(relative)
    for b in preset.blocks:
        if b is history_marker:
            messages.extend(chat_msgs)
        elif b.id in prepared and b.injection_position != POSITION_IN_CHAT:
            messages.append(next(rel_iter))

    # 5.
    if preset.options.squash_system_messages:
        messages = _squash(messages)
    messages = [m for m in messages if m.content]

    # 6.
    messages = post_process(messages, preset.options.post_processing, preset.options.prompt_placeholder)

    if history_marker is None and history:
        ctx.warnings.append("沒有啟用「聊天歷史」marker：聊天歷史與深度注入都不會送出。")
    if history_marker is not None and history_included < len(history):
        ctx.warnings.append(f"預算不足：聊天歷史只放入最新 {history_included} / {len(history)} 則。")
    if used > budget:
        ctx.warnings.append(f"區塊本身已超出預算（{used} > {budget} tokens）；ST 會在此中止組裝。")

    return BuildResult(
        messages=messages,
        budget=budget,
        history_total=len(history),
        history_included=history_included,
        variables=ctx.variables,
        block_tokens={bid: tokens.count(text) for bid, text in prepared.items()},
        warnings=ctx.warnings,
    )


def _inject(absolute: list[Block], prepared: dict[str, str], history: list[HistoryMessage]) -> list[Msg]:
    """ST populationInjectionPrompts：依深度把注入區塊插進歷史，回傳由舊到新的列表。

    同一深度內：injection_order 大的先處理（最後會較靠近結尾），同 order 內依
    system → user → assistant 處理並以 \\n 合併；插入「由新到舊」的陣列後整體反轉。
    淨效果：depth 0 = 最後一則之後；同 order 時實際順序為 assistant、user、system。
    """
    newest_first = [
        Msg(h.role, h.content, [Source("history", name=f"#{i + 1}")], h.name)
        for i, h in reversed(list(enumerate(history)))
    ]
    by_depth: dict[int, list[Block]] = {}
    for b in absolute:
        if prepared[b.id]:
            by_depth.setdefault(min(max(b.injection_depth, 0), MAX_INJECTION_DEPTH), []).append(b)

    inserted = 0
    for depth in sorted(by_depth):
        groups: dict[int, list[Block]] = {}
        for b in by_depth[depth]:
            groups.setdefault(b.injection_order, []).append(b)

        role_msgs: list[Msg] = []
        for order in sorted(groups, reverse=True):
            for role in ROLE_PRIORITY:
                blocks = [b for b in groups[order] if b.role == role]
                if not blocks:
                    continue
                content = "\n".join(prepared[b.id].strip() for b in blocks)
                if content:
                    role_msgs.append(Msg(role, content, [Source("injection", b.id, b.name) for b in blocks]))

        idx = depth + inserted
        newest_first[idx:idx] = role_msgs
        inserted += len(role_msgs)

    newest_first.reverse()
    return newest_first


def _squash(messages: list[Msg]) -> list[Msg]:
    """ST ChatCompletion.squashSystemMessages。"""

    def squashable(m: Msg) -> bool:
        return m.squashable and m.role == "system" and not m.name

    out: list[Msg] = []
    last: Msg | None = None
    for m in messages:
        if m.role == "system" and not m.content:
            continue
        if squashable(m) and last is not None and squashable(last):
            last.content += "\n" + m.content
            last.sources = last.sources + m.sources
            continue
        m = Msg(m.role, m.content, list(m.sources), m.name, m.squashable)
        out.append(m)
        last = m
    return out


def post_process(messages: list[Msg], mode: str, placeholder: str) -> list[Msg]:
    """ST prompt-converters.js postProcessPrompt / mergeMessages（不含工具與多媒體）。"""
    if mode == "merge":
        return _merge(messages, strict=False, placeholders=False, single=False, placeholder=placeholder)
    if mode == "semi":
        return _merge(messages, strict=True, placeholders=False, single=False, placeholder=placeholder)
    if mode == "strict":
        return _merge(messages, strict=True, placeholders=True, single=False, placeholder=placeholder)
    if mode == "single":
        return _merge(messages, strict=True, placeholders=False, single=True, placeholder=placeholder)
    return messages


def _merge(messages: list[Msg], *, strict: bool, placeholders: bool, single: bool, placeholder: str) -> list[Msg]:
    msgs = []
    for m in messages:
        content = m.content
        if m.name and m.role != "system" and not content.startswith(f"{m.name}: "):
            content = f"{m.name}: {content}"
        msgs.append(Msg("user" if single else m.role, content, list(m.sources)))

    merged: list[Msg] = []
    for m in msgs:
        if merged and merged[-1].role == m.role and m.content:
            merged[-1].content += "\n\n" + m.content
            merged[-1].sources += m.sources
        else:
            merged.append(m)

    if not merged:
        merged.append(Msg("user", placeholder, [Source("placeholder", name="prompt_placeholder")]))

    if strict:
        for i, m in enumerate(merged):
            if i > 0 and m.role == "system":
                m.role = "user"
        if merged and placeholders:
            ph = Msg("user", placeholder, [Source("placeholder", name="prompt_placeholder")])
            if merged[0].role == "system" and (len(merged) == 1 or merged[1].role != "user"):
                merged.insert(1, ph)
            elif merged[0].role not in ("system", "user"):
                merged.insert(0, ph)
        return _merge(merged, strict=False, placeholders=placeholders, single=False, placeholder=placeholder)

    return merged
