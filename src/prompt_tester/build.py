"""把預設 + 情境組成 Chat Completion messages[]。

組裝順序依 SillyTavern 1.18.0（public/scripts/openai.js），詳見 doc/design.md §5.2：
1. 依列表順序展開啟用的內容區塊巨集（相對與深度注入都在這一步展開）
2. 深度注入插入聊天歷史（populationInjectionPrompts）
3. 從最新往最舊放入歷史，超出預算即停止；最前面加 new_chat_prompt（populateChatHistory）
4. 依列表順序排出最終訊息；沒有啟用 chatHistory marker 就完全不送歷史與深度注入
5. squash_system_messages、去掉空訊息（getChat）
6. 後處理（src/prompt-converters.js postProcessPrompt）
"""

import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from . import tokens
from .entities import CharacterCard
from .macros import MacroContext, evaluate
from .preset import POSITION_IN_CHAT, Block, HistoryMessage, Preset
from .worldinfo import WIResult, WISettings, activate

MAX_INJECTION_DEPTH = 10000
ROLE_PRIORITY = ("system", "user", "assistant")
# ST populateChatCompletion 會送出的 system_prompt 內容區塊；其他 system_prompt: true 的區塊不送（oa:1203-1249）
ST_SYSTEM_RELATIVE = {"main", "nsfw", "jailbreak", "enhanceDefinitions"}
GENERATION_TYPE = "normal"


def _is_active(b: Block) -> bool:
    """啟用、是內容區塊，且 injection_trigger 適用於本次生成類型（空 = 全部適用）。"""
    triggers = b.st.get("injection_trigger") or []
    return b.kind == "content" and b.enabled and (not triggers or GENERATION_TYPE in triggers)


def _sends_relative(b: Block) -> bool:
    return b.injection_position != POSITION_IN_CHAT and (
        not b.st.get("system_prompt", False) or b.id in ST_SYSTEM_RELATIVE
    )

Origin = Literal["block", "history", "injection", "separator", "placeholder", "card", "worldinfo", "example"]


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
    worldinfo: WIResult | None = None

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
    card: CharacterCard | None = None,
    persona: str = "",
) -> BuildResult:
    opts = preset.options
    names = dict(names)
    if card:
        # ST 角色卡相關巨集（{{description}}、{{personality}}…）
        names.update(
            description=card.description,
            personality=card.personality,
            scenario=card.scenario,
            mesExamples=card.mes_example,
            mesExamplesRaw=card.mes_example,
            charPrompt=card.system_prompt,
            charInstruction=card.post_history_instructions,
            charDepthPrompt=card.depth_prompt.prompt,
            creatorNotes=card.creator_notes,
        )
    names["persona"] = persona

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
    budget = preset.params.max_context - preset.params.max_tokens - tokens.REPLY_PRIMING

    # 0. 角色卡欄位第一次巨集展開（ST baseChatReplace）與世界書觸發（sc:4402-4622）
    card_fields: dict[str, str] = {}
    wi: WIResult | None = None
    if card:
        for key in ("description", "personality", "scenario", "mes_example", "system_prompt",
                    "post_history_instructions"):
            card_fields[key] = expand(getattr(card, key))
        card_fields["depth_prompt"] = expand(card.depth_prompt.prompt)
        if card.book and card.book.entries:
            char_name, user_name = names.get("char", ""), names.get("user", "")
            lines = []
            for h in reversed(history):
                if h.role == "user":
                    speaker = h.name or user_name
                elif h.role == "assistant":
                    speaker = h.name or char_name
                else:
                    speaker = h.name or "System"
                lines.append((f"{speaker}: {h.content}" if opts.wi_include_names else h.content).strip())
            wi = activate(
                card.book,
                lines,
                sources={
                    "persona_description": persona,
                    "character_description": card.description,
                    "character_personality": card.personality,
                    "character_depth_prompt": card.depth_prompt.prompt,
                    "scenario": card.scenario,
                    "creator_notes": card.creator_notes,
                },
                settings=WISettings(
                    scan_depth=opts.wi_scan_depth,
                    budget_percent=opts.wi_budget_percent,
                    budget_cap=opts.wi_budget_cap,
                    recursive=opts.wi_recursive,
                    case_sensitive=opts.wi_case_sensitive,
                    match_whole_words=opts.wi_match_whole_words,
                    include_names=opts.wi_include_names,
                ),
                max_prompt_tokens=preset.params.max_context - preset.params.max_tokens,
                expand=expand,
                rng=random.Random(f"wi-{seed}"),
                chat_length=len(history),
            )
            ctx.outlets = wi.outlets
            ctx.warnings.extend(wi.warnings)

    # 1. 展開區塊（ST getPromptCollection：依 prompt_order 順序，每個區塊一次）
    prepared: dict[str, str] = {}
    for b in preset.blocks:
        if _is_active(b):
            prepared[b.id] = expand(b.content)

    # 1b. 角色卡覆寫 main / jailbreak（oa:1497-1515），{{original}} = 預設原文
    if card:
        for ident, key, prefer in (
            ("main", "system_prompt", opts.prefer_character_prompt),
            ("jailbreak", "post_history_instructions", opts.prefer_character_jailbreak),
        ):
            blk = next((b for b in preset.blocks if b.id == ident), None)
            if (
                prefer
                and card_fields.get(key)
                and blk is not None
                and blk.id in prepared
                and not blk.st.get("forbid_overrides")
            ):
                ctx.original = prepared[ident]
                prepared[ident] = expand(card_fields[key])
                ctx.original = None

    # 1c. marker 內容（第二次巨集展開，對應 ST 合併時的 preparePrompt）
    marker_text: dict[str, str] = {}
    marker_source: dict[str, str] = {}
    if card:
        if card_fields["description"]:
            marker_text["charDescription"] = expand(card_fields["description"])
        if card_fields["personality"]:
            ctx.names["personality"] = card_fields["personality"]
            marker_text["charPersonality"] = expand(expand(opts.personality_format))
        if card_fields["scenario"]:
            ctx.names["scenario"] = card_fields["scenario"]
            marker_text["scenario"] = expand(expand(opts.scenario_format))
        marker_source.update(charDescription="card", charPersonality="card", scenario="card")
    if persona:
        marker_text["personaDescription"] = expand(expand(persona))
        marker_source["personaDescription"] = "card"
    if wi:
        for marker, text in (("worldInfoBefore", wi.before), ("worldInfoAfter", wi.after)):
            if text:
                marker_text[marker] = expand(_format_wi(opts.wi_format, text))
                marker_source[marker] = "worldinfo"

    # 範例對話區塊（每塊：分隔 + 訊息），預算排在聊天歷史之後
    example_blocks: list[list[Msg]] = []
    raw_blocks = parse_mes_examples(card_fields.get("mes_example", "")) if card else []
    if wi:
        raw_blocks = [b for t in wi.em_top for b in parse_mes_examples(expand(t))] + raw_blocks
        raw_blocks += [b for t in wi.em_bottom for b in parse_mes_examples(expand(t))]
    no_char_lines = []
    for i, blk_text in enumerate(raw_blocks):
        msgs = parse_example_block(blk_text, names.get("user", ""), names.get("char", ""))
        if msgs:
            example_blocks.append(msgs)
            if not any(m.name == "example_assistant" for m in msgs):
                no_char_lines.append(i + 1)
    if no_char_lines:
        ctx.warnings.append(
            f"範例對話第 {'、'.join(map(str, no_char_lines))} 段沒有辨識到角色台詞，全部會被當成 {{{{user}}}} 的話。"
            f"ST 只認「{names.get('char', '')}:」開頭（角色名 + 半形冒號），建議寫成 {{{{char}}}}:。"
        )

    history_marker = next(
        (b for b in preset.blocks if b.kind == "marker" and b.marker == "chatHistory" and b.enabled),
        None,
    )
    examples_marker = next(
        (b for b in preset.blocks if b.kind == "marker" and b.marker == "dialogueExamples" and b.enabled),
        None,
    )

    relative: dict[str, Msg] = {}
    for b in preset.blocks:
        if b.id in prepared and _sends_relative(b):
            relative[b.id] = Msg(b.role, prepared[b.id], [Source("block", b.id, b.name)])
        elif b.kind == "marker" and b.enabled and b.marker in marker_text:
            origin = marker_source.get(b.marker, "card")
            relative[b.id] = Msg(b.role, marker_text[b.marker], [Source(origin, b.id, b.name)])
    used = sum(m.tokens for m in relative.values())

    # 2-3. 聊天歷史與深度注入
    chat_msgs: list[Msg] = []
    history_included = 0
    remaining = budget - used
    if history_marker is not None:
        absolute = [b for b in preset.blocks if b.id in prepared and b.injection_position == POSITION_IN_CHAT]
        extension: list[tuple[str, int, str, str, Source]] = []
        if card and card_fields["depth_prompt"]:
            dp = card.depth_prompt
            extension.append(
                ("DEPTH_PROMPT", dp.depth, dp.role, card_fields["depth_prompt"], Source("card", None, "角色卡深度提示"))
            )
        if wi:
            for d, role, text in wi.depth:
                key = f"customDepthWI_{d}_{ROLE_PRIORITY.index(role)}"
                extension.append((key, d, role, text, Source("worldinfo", None, f"世界書 @深度 {d}")))
        pool = _inject(absolute, prepared, history, extension, expand)

        separator = Msg(
            "system", expand(opts.new_chat_prompt), [Source("separator", name="new_chat_prompt")], squashable=False
        )
        remaining -= separator.tokens
        for item in reversed(pool):
            msg = Msg(item.role, expand(item.content), item.sources, item.name)
            if msg.tokens > remaining:
                break
            remaining -= msg.tokens
            chat_msgs.insert(0, msg)
            if msg.sources[0].origin == "history":
                history_included += 1
        chat_msgs.insert(0, separator)

    examples_out: list[Msg] = []
    examples_included = 0
    if examples_marker is not None and example_blocks:
        sep_text = expand(opts.new_example_chat_prompt)
        for i, msgs in enumerate(example_blocks):
            sep = Msg("system", sep_text, [Source("example", examples_marker.id, f"範例 {i + 1}")], squashable=False)
            cost = sep.tokens + sum(m.tokens for m in msgs)
            if cost > remaining:
                break
            remaining -= cost
            examples_out += [sep, *msgs]
            examples_included += 1

    # 4. 依列表順序排出
    messages: list[Msg] = []
    for b in preset.blocks:
        if b is history_marker:
            messages.extend(chat_msgs)
        elif b is examples_marker:
            messages.extend(examples_out)
        elif b.id in relative:
            messages.append(relative[b.id])

    # 5.
    if opts.squash_system_messages:
        messages = _squash(messages)
    messages = [m for m in messages if m.content]

    # 6.
    messages = post_process(messages, opts.post_processing, opts.prompt_placeholder)

    if history_marker is None and history:
        ctx.warnings.append("沒有啟用「聊天歷史」marker：聊天歷史與深度注入都不會送出。")
    if history_marker is not None and history_included < len(history):
        ctx.warnings.append(f"預算不足：聊天歷史只放入最新 {history_included} / {len(history)} 則。")
    if examples_marker is not None and examples_included < len(example_blocks):
        ctx.warnings.append(f"預算不足：範例對話只放入 {examples_included} / {len(example_blocks)} 段。")
    if example_blocks and examples_marker is None:
        ctx.warnings.append("角色卡有範例對話，但預設沒有啟用「範例對話」marker，不會送出。")
    if used > budget:
        ctx.warnings.append(f"區塊本身已超出預算（{used} > {budget} tokens）；ST 會在此中止組裝。")
    prompt_only = [
        s.get("scriptName") or "(未命名)"
        for s in [*((preset.extra.get("extensions") or {}).get("regex_scripts") or []), *(card.regex_scripts if card else [])]
        if isinstance(s, dict) and s.get("promptOnly") and not s.get("disabled")
    ]
    if prompt_only:
        ctx.warnings.append(
            f"{len(prompt_only)} 個「只改送出內容」（promptOnly）的正則尚未套用到送出的 messages：{'、'.join(prompt_only[:5])}"
        )

    block_tokens = {bid: tokens.count(text) for bid, text in prepared.items()}
    for b in preset.blocks:
        if b.kind == "marker" and b.marker in marker_text:
            block_tokens[b.id] = tokens.count(marker_text[b.marker])
        elif b is examples_marker and examples_out:
            block_tokens[b.id] = sum(m.tokens for m in examples_out)

    return BuildResult(
        messages=messages,
        budget=budget,
        history_total=len(history),
        history_included=history_included,
        variables=ctx.variables,
        block_tokens=block_tokens,
        warnings=ctx.warnings,
        worldinfo=wi,
    )


def _format_wi(fmt: str, value: str) -> str:
    """ST formatWorldInfo：格式空白時原樣，否則以 {0} 代入。"""
    return value if not fmt.strip() else fmt.replace("{0}", value)


def parse_mes_examples(text: str) -> list[str]:
    """ST parseMesExamples（Chat Completion）：每段回傳 "<START>\n…\n"。"""
    if not text or text == "<START>":
        return []
    if not text.startswith("<START>"):
        text = "<START>\n" + text.strip()
    return [f"<START>\n{block.strip()}\n" for block in re.split(r"<START>", text, flags=re.I)[1:]]


def parse_example_block(block: str, user_name: str, char_name: str) -> list[Msg]:
    """ST setOpenAIMessageExamples + parseExampleIntoIndividual：拆成具名的 system 訊息。"""
    block = re.sub(r"<START>", "{Example Dialogue:}", block, count=1, flags=re.I).replace("\r", "")
    out: list[Msg] = []
    cur: list[str] = []
    in_user = in_bot = False

    def add(name: str, system_name: str) -> None:
        nonlocal cur
        content = "\n".join(cur).replace(f"{name}:", "", 1).strip()
        out.append(Msg("system", content, [Source("example", None, system_name)], system_name))
        cur = []

    for line in block.split("\n")[1:]:
        if line.startswith(f"{user_name}:"):
            in_user = True
            if in_bot:
                add(char_name, "example_assistant")
            in_bot = False
        elif line.startswith(f"{char_name}:"):
            in_bot = True
            if in_user:
                add(user_name, "example_user")
            in_user = False
        cur.append(line)
    if in_user:
        add(user_name, "example_user")
    elif in_bot:
        add(char_name, "example_assistant")
    return out


def _inject(
    absolute: list[Block],
    prepared: dict[str, str],
    history: list[HistoryMessage],
    extension: list[tuple[str, int, str, str, Source]],
    expand: Callable[[str], str],
) -> list[Msg]:
    """ST populationInjectionPrompts：依深度把注入區塊插進歷史，回傳由舊到新的列表。

    同一深度內：injection_order 大的先處理（最後會較靠近結尾），同 order 內依
    system → user → assistant 處理並以 \\n 合併；插入「由新到舊」的陣列後整體反轉。
    淨效果：depth 0 = 最後一則之後；同 order 時實際順序為 assistant、user、system。
    擴充提示（角色卡深度提示、世界書 @深度）只併入 order 100 的群組，依 key 字母順序
    排在區塊之後，並再展開一次巨集（getExtensionPrompt，sc:3242）。
    """
    newest_first = [
        Msg(h.role, h.content, [Source("history", name=f"#{i + 1}")], h.name)
        for i, h in reversed(list(enumerate(history)))
    ]
    by_depth: dict[int, list[Block]] = {}
    for b in absolute:
        if prepared[b.id]:
            by_depth.setdefault(min(max(b.injection_depth, 0), MAX_INJECTION_DEPTH), []).append(b)
    ext_by_depth: dict[int, list[tuple[str, int, str, str, Source]]] = {}
    for item in extension:
        ext_by_depth.setdefault(min(max(item[1], 0), MAX_INJECTION_DEPTH), []).append(item)

    inserted = 0
    for depth in sorted(set(by_depth) | set(ext_by_depth)):
        groups: dict[int, list[Block]] = {100: []}
        for b in by_depth.get(depth, []):
            groups.setdefault(b.injection_order, []).append(b)

        role_msgs: list[Msg] = []
        for order in sorted(groups, reverse=True):
            for role in ROLE_PRIORITY:
                blocks = [b for b in groups[order] if b.role == role]
                parts = [prepared[b.id] for b in blocks]
                sources = [Source("injection", b.id, b.name) for b in blocks]
                if order == 100:
                    exts = sorted((e for e in ext_by_depth.get(depth, []) if e[2] == role), key=lambda e: e[0])
                    ext_text = "\n".join(e[3].strip() for e in exts if e[3].strip())
                    if ext_text:
                        parts.append(expand(ext_text))
                        sources += [e[4] for e in exts]
                content = "\n".join(x.strip() for x in parts if x and x.strip())
                if content:
                    role_msgs.append(Msg(role, content, sources))

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
