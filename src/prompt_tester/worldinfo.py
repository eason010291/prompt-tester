"""世界書（World Info）：角色卡 character_book 轉換與單次生成的觸發。

依 SillyTavern 1.18.0 public/scripts/world-info.js（WI:行號）實作單次生成、無聊天狀態的子集：
- 卡片轉換 convertCharacterBook（WI:5498-5555），包括與範本不同的預設值
- 掃描文字：最新 N 則「名字: 內容」，以 \\x01 分隔（WI:279-328）；深度 <= 0 不比對任何東西
- 關鍵字：巨集展開後 trim；/regex/flags 對原文比對；一般關鍵字依大小寫設定，
  全字比對使用 ASCII \\W（JS 無 u 旗標），所以中文關鍵字等同子字串比對（WI:337-366）
- 選擇邏輯 AND_ANY / NOT_ALL / NOT_ANY / AND_ALL（WI:4812-4866）
- 迴圈：常駐、@@activate / @@dont_activate、triggers、delay、群組、機率（常駐也會擲）、
  預算（貪婪、超出即停）、遞迴（prevent / exclude / delay_until_recursion）
- 輸出：依 order 由小到大（越大越靠近聊天），before / after / @深度 / 範例上下 / outlet

省略（需要聊天狀態或本工具沒有的功能）：sticky、cooldown、外部觸發、角色過濾、
min_activations、WORLD_INFO 正則；AN 位置的條目不會送出（本工具沒有 Author's Note）。
"""

import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import tokens
from .entities import LoreEntry, Lorebook

POS_BEFORE, POS_AFTER, POS_AN_TOP, POS_AN_BOTTOM, POS_DEPTH, POS_EM_TOP, POS_EM_BOTTOM, POS_OUTLET = range(8)
AND_ANY, NOT_ALL, NOT_ANY, AND_ALL = range(4)
ROLES = ("system", "user", "assistant")
MATCH_SOURCES = {
    "match_persona_description": "persona_description",
    "match_character_description": "character_description",
    "match_character_personality": "character_personality",
    "match_character_depth_prompt": "character_depth_prompt",
    "match_scenario": "scenario",
    "match_creator_notes": "creator_notes",
}
# ST buffer.get 附加順序
SOURCE_ORDER = [
    "persona_description",
    "character_description",
    "character_personality",
    "character_depth_prompt",
    "scenario",
    "creator_notes",
]
GENERATION_TYPE = "normal"


# ---- 卡片轉換 ----

def lorebook_from_card(book: dict) -> tuple[Lorebook, list[str]]:
    notes: list[str] = []
    entries = []
    missing_enabled = 0
    for i, e in enumerate(book.get("entries") or []):
        if not isinstance(e, dict):
            continue
        x = e.get("extensions") or {}
        if "enabled" not in e:
            missing_enabled += 1
        position = x.get("position")
        if position is None:
            position = POS_BEFORE if e.get("position") == "before_char" else POS_AFTER
        dur = x.get("delay_until_recursion")
        entries.append(
            LoreEntry(
                uid=int(e["id"]) if isinstance(e.get("id"), int) else i,
                comment=e.get("comment") or "",
                keys=[k for k in e.get("keys") or [] if isinstance(k, str)],
                secondary_keys=[k for k in e.get("secondary_keys") or [] if isinstance(k, str)],
                content=e.get("content") or "",
                constant=bool(e.get("constant")),
                selective=bool(e.get("selective")),
                selective_logic=int(x.get("selectiveLogic") or 0),
                enabled=bool(e.get("enabled", False)),
                order=int(e.get("insertion_order") or 0),
                position=int(position),
                depth=int(x.get("depth", 4) if x.get("depth") is not None else 4),
                role=int(x.get("role") or 0),
                probability=int(x.get("probability", 100) if x.get("probability") is not None else 100),
                use_probability=bool(x.get("useProbability", True)),
                scan_depth=x.get("scan_depth"),
                case_sensitive=x.get("case_sensitive"),
                match_whole_words=x.get("match_whole_words"),
                exclude_recursion=bool(x.get("exclude_recursion")),
                prevent_recursion=bool(x.get("prevent_recursion")),
                delay_until_recursion=(1 if dur is True else int(dur or 0)),
                group=x.get("group") or "",
                group_override=bool(x.get("group_override")),
                group_weight=int(x.get("group_weight") or 100),
                use_group_scoring=x.get("use_group_scoring"),
                ignore_budget=bool(x.get("ignore_budget")),
                outlet_name=x.get("outlet_name") or "",
                triggers=[t for t in x.get("triggers") or [] if isinstance(t, str)],
                match_sources=[src for flag, src in MATCH_SOURCES.items() if x.get(flag)],
                delay=x.get("delay"),
            )
        )
    if missing_enabled:
        notes.append(f"{missing_enabled} 個世界書條目沒有 enabled 欄位，依 ST 規則視為停用。")
    an = [e for e in entries if e.enabled and e.position in (POS_AN_TOP, POS_AN_BOTTOM)]
    if an:
        notes.append(f"{len(an)} 個世界書條目放在 Author's Note 位置，本工具沒有 AN，這些條目不會送出。")
    return (
        Lorebook(
            name=book.get("name") or "",
            entries=entries,
            scan_depth=book.get("scan_depth"),
            token_budget=book.get("token_budget"),
            recursive_scanning=book.get("recursive_scanning"),
        ),
        notes,
    )


# ---- 觸發 ----

@dataclass
class WISettings:
    """ST 全新安裝的預設值（settings.json 讓 recursive 與 match_whole_words 為 true）。"""

    scan_depth: int = 2
    budget_percent: int = 25
    budget_cap: int = 0
    recursive: bool = True
    case_sensitive: bool = False
    match_whole_words: bool = True
    include_names: bool = True
    max_recursion_steps: int = 0
    use_group_scoring: bool = False


@dataclass
class Activated:
    entry: LoreEntry
    content: str
    reason: str  # constant | key:<關鍵字> | decorator


@dataclass
class WIResult:
    before: str = ""
    after: str = ""
    # (depth, role 名稱, 內容)
    depth: list[tuple[int, str, str]] = field(default_factory=list)
    em_top: list[str] = field(default_factory=list)
    em_bottom: list[str] = field(default_factory=list)
    outlets: dict[str, str] = field(default_factory=dict)
    activated: list[Activated] = field(default_factory=list)
    budget: int = 0
    used_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


_REGEX_KEY = re.compile(r"^/(.+?)/([gimsuy]*)$", re.S)


def _parse_regex(key: str) -> re.Pattern | None:
    m = _REGEX_KEY.match(key)
    if not m:
        return None
    pattern, flags = m.group(1), m.group(2)
    if re.search(r"(?<!\\)/", pattern):  # 未跳脫的 / 視為不是 regex
        return None
    pattern = pattern.replace("\\/", "/", 1)
    f = 0
    if "i" in flags:
        f |= re.I
    if "m" in flags:
        f |= re.M
    if "s" in flags:
        f |= re.S
    try:
        return re.compile(pattern, f)
    except re.error:
        return None


def _match_key(hay: str, key: str, case_sensitive: bool, whole_words: bool) -> bool:
    rx = _parse_regex(key)
    if rx is not None:
        return rx.search(hay) is not None
    if not case_sensitive:
        hay, key = hay.lower(), key.lower()
    if not whole_words or len(key.split()) > 1:
        return key in hay
    return re.search(r"(?:^|\W)(" + re.escape(key) + r")(?:$|\W)", hay, re.ASCII) is not None


def _decorators(content: str) -> tuple[set[str], str]:
    if not content.startswith("@@"):
        return set(), content
    lines = content.split("\n")
    decos = set()
    while lines and lines[0].strip() in ("@@activate", "@@dont_activate"):
        decos.add(lines.pop(0).strip())
    return decos, "\n".join(lines)


def activate(
    book: Lorebook,
    chat_newest_first: list[str],
    *,
    sources: dict[str, str],
    settings: WISettings,
    max_prompt_tokens: int,
    expand: Callable[[str], str],
    rng: random.Random,
    chat_length: int,
) -> WIResult:
    """chat_newest_first：已格式化（"名字: 內容"、trim）的訊息，索引 0 為最新。"""
    res = WIResult()
    depth_default = book.scan_depth if book.scan_depth is not None else settings.scan_depth
    recursive = book.recursive_scanning if book.recursive_scanning is not None else settings.recursive
    budget = round(settings.budget_percent * max_prompt_tokens / 100) or 1
    if book.token_budget:
        budget = min(budget, book.token_budget)
    if settings.budget_cap > 0:
        budget = min(budget, settings.budget_cap)
    res.budget = budget

    entries = sorted(book.entries, key=lambda e: -e.order)  # 穩定排序：order 大的先
    activated: dict[int, Activated] = {}
    failed: set[int] = set()
    activated_groups: set[str] = set()
    recursion_buffer: list[str] = []
    all_text = ""
    overflow = False
    state = "initial"
    levels = sorted({e.delay_until_recursion for e in entries if e.delay_until_recursion})
    level = levels[0] if levels else 0
    steps = 0

    def scan_text(e: LoreEntry) -> str:
        depth = e.scan_depth if e.scan_depth is not None else depth_default
        if depth <= 0:
            return ""
        parts = chat_newest_first[:depth]
        text = "\x01" + "\n\x01".join(parts)
        for src in SOURCE_ORDER:
            if src in e.match_sources and sources.get(src):
                text += "\n\x01" + sources[src]
        if state == "recursion" and recursion_buffer:
            text += "\n\x01" + "\n\x01".join(recursion_buffer)
        return text

    def matches(e: LoreEntry, text: str, key: str) -> bool:
        key = expand(key).strip()
        if not key:
            return False
        cs = e.case_sensitive if e.case_sensitive is not None else settings.case_sensitive
        ww = e.match_whole_words if e.match_whole_words is not None else settings.match_whole_words
        return _match_key(text, key, cs, ww)

    while True:
        steps += 1
        candidates: list[tuple[LoreEntry, str]] = []
        scores: dict[int, int] = {}
        for e in entries:
            if e.uid in activated or e.uid in failed or not e.enabled:
                continue
            if e.triggers and GENERATION_TYPE not in e.triggers:
                continue
            if e.delay and chat_length < e.delay:
                continue
            if e.delay_until_recursion and (state != "recursion" or e.delay_until_recursion > level):
                continue
            if state == "recursion" and recursive and e.exclude_recursion:
                continue
            decos, _ = _decorators(e.content)
            if "@@dont_activate" in decos:
                continue
            if "@@activate" in decos:
                candidates.append((e, "decorator"))
                continue
            if e.constant:
                candidates.append((e, "constant"))
                continue
            if not e.keys:
                continue
            text = scan_text(e)
            if not text:
                continue
            hit = [k for k in e.keys if matches(e, text, k)]
            if not hit:
                continue
            if e.selective and e.secondary_keys:
                sec = [matches(e, text, k) for k in e.secondary_keys]
                ok = {
                    AND_ANY: any(sec),
                    NOT_ALL: not all(sec),
                    NOT_ANY: not any(sec),
                    AND_ALL: all(sec),
                }.get(e.selective_logic, any(sec))
                if not ok:
                    continue
                bonus = sum(sec) if e.selective_logic == AND_ANY else (len(sec) if e.selective_logic == AND_ALL else 0)
            else:
                bonus = 0
            scores[e.uid] = len(hit) + bonus
            candidates.append((e, f"key:{hit[0]}"))

        candidates = _apply_groups(candidates, scores, activated_groups, settings, rng)

        successful: list[Activated] = []
        new_content = ""
        for e, reason in candidates:
            if e.use_probability and e.probability < 100 and rng.random() * 100 > e.probability:
                failed.add(e.uid)
                continue
            if overflow and not e.ignore_budget:
                continue
            content = expand(_decorators(e.content)[1])
            new_content += content + "\n"
            if not e.ignore_budget and tokens.count(all_text) + tokens.count(new_content) >= budget:
                overflow = True
                res.warnings.append(f"世界書超出預算（{budget} tokens），「{e.comment or e.uid}」及之後的條目未送出。")
                continue
            a = Activated(e, content, reason)
            activated[e.uid] = a
            successful.append(a)
            for g in re.split(r",\s*", e.group) if e.group else []:
                activated_groups.add(g)

        if settings.max_recursion_steps and steps >= settings.max_recursion_steps:
            break
        recursable = [a.content for a in successful if not a.entry.prevent_recursion]
        if recursive and not overflow and recursable:
            recursion_buffer.extend(recursable)
            all_text = "\n".join(recursion_buffer)
            state = "recursion"
            continue
        pending = [lv for lv in levels if lv > level]
        if pending:
            level = pending[0]
            state = "recursion"
            recursion_buffer.extend(recursable)
            all_text = "\n".join(recursion_buffer)
            continue
        break

    # 依 order 由大到小排序後逐一 unshift：結果為由小到大，同 order 時後觸發的在前
    ordered = list(reversed(sorted(activated.values(), key=lambda a: -a.entry.order)))
    res.activated = ordered
    res.used_tokens = tokens.count("\n".join(a.content for a in ordered))

    before, after = [], []
    depth_groups: dict[tuple[int, int], list[str]] = {}
    for a in ordered:
        if not a.content:
            continue
        pos = a.entry.position
        if pos == POS_BEFORE:
            before.append(a.content)
        elif pos == POS_AFTER:
            after.append(a.content)
        elif pos == POS_DEPTH:
            depth_groups.setdefault((a.entry.depth, a.entry.role), []).append(a.content)
        elif pos == POS_EM_TOP:
            res.em_top.append(a.content)
        elif pos == POS_EM_BOTTOM:
            res.em_bottom.append(a.content)
    for a in reversed(ordered):  # outlet 用 push：由大到小
        if a.entry.position == POS_OUTLET and a.entry.outlet_name and a.content:
            name = a.entry.outlet_name
            res.outlets[name] = (res.outlets[name] + "\n" + a.content) if name in res.outlets else a.content
    res.before = "\n".join(before)
    res.after = "\n".join(after)
    res.depth = [
        (d, ROLES[r] if 0 <= r < 3 else "system", "\n".join(c)) for (d, r), c in sorted(depth_groups.items())
    ]
    return res


def _apply_groups(
    candidates: list[tuple[LoreEntry, str]],
    scores: dict[int, int],
    activated_groups: set[str],
    settings: WISettings,
    rng: random.Random,
) -> list[tuple[LoreEntry, str]]:
    groups: dict[str, list[LoreEntry]] = {}
    for e, _ in candidates:
        for g in re.split(r",\s*", e.group) if e.group else []:
            groups.setdefault(g, []).append(e)
    if not groups:
        return candidates

    removed: set[int] = set()
    for name, members in groups.items():
        members = [m for m in members if m.uid not in removed]
        if not members:
            continue
        scoring = [m for m in members if (m.use_group_scoring if m.use_group_scoring is not None else settings.use_group_scoring)]
        if scoring:
            best = max(scores.get(m.uid, 0) for m in scoring)
            for m in scoring:
                if scores.get(m.uid, 0) < best:
                    removed.add(m.uid)
            members = [m for m in members if m.uid not in removed]
        if name in activated_groups:
            removed.update(m.uid for m in members)
            continue
        if len(members) <= 1:
            continue
        overrides = [m for m in members if m.group_override]
        if overrides:
            winner = max(overrides, key=lambda m: m.order)
        else:
            total = sum(m.group_weight for m in members)
            roll = rng.random() * total
            winner = members[-1]
            for m in members:
                roll -= m.group_weight
                if roll <= 0:
                    winner = m
                    break
        removed.update(m.uid for m in members if m is not winner)
    return [(e, r) for e, r in candidates if e.uid not in removed]
