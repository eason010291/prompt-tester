import random

import pytest

from prompt_tester.entities import LoreEntry, Lorebook
from prompt_tester.worldinfo import (
    AND_ALL,
    AND_ANY,
    NOT_ALL,
    NOT_ANY,
    POS_AFTER,
    POS_BEFORE,
    POS_DEPTH,
    POS_OUTLET,
    WISettings,
    _match_key,
    activate,
    lorebook_from_card,
)


def entry(uid, content="", **kw) -> LoreEntry:
    kw.setdefault("enabled", True)
    kw.setdefault("position", POS_BEFORE)
    return LoreEntry(uid=uid, comment=f"e{uid}", content=content or f"內容{uid}", **kw)


def run(entries, chat=("使用者: 你好",), **settings_kw):
    return activate(
        Lorebook(entries=entries),
        list(chat),
        sources={},
        settings=WISettings(**settings_kw),
        max_prompt_tokens=50000,
        expand=lambda t: t,
        rng=random.Random(1),
        chat_length=len(chat),
    )


def ids(res):
    return [a.entry.uid for a in res.activated]


# ---- 卡片轉換 ----

def test_card_conversion_defaults_follow_st():
    book, notes = lorebook_from_card({"name": "書", "entries": [
        {"id": 0, "keys": ["a"], "content": "x", "enabled": True, "position": "before_char", "insertion_order": 5,
         "extensions": {"position": 1, "depth": 2, "role": 1, "probability": 50, "useProbability": True,
                        "selectiveLogic": 3, "match_character_description": True, "delay_until_recursion": True}},
        {"id": 1, "keys": ["b"], "content": "y"},  # 沒有 enabled、position、selective
    ]})
    a, b = book.entries
    assert a.position == POS_AFTER  # extensions.position 優先於字串
    assert (a.depth, a.role, a.probability, a.selective_logic, a.order) == (2, 1, 50, AND_ALL, 5)
    assert a.match_sources == ["character_description"] and a.delay_until_recursion == 1
    assert b.enabled is False and b.position == POS_AFTER and b.selective is False and b.order == 0
    assert any("enabled" in n for n in notes)


# ---- 關鍵字比對 ----

@pytest.mark.parametrize(
    "hay, key, cs, ww, expected",
    [
        ("\x01使用者: 我們去買東西", "買", False, True, True),  # 中文：ASCII \W 讓 CJK 視為邊界，等同子字串
        ("\x01user: catapult", "cat", False, True, False),  # 英文全字比對
        ("\x01user: a cat here", "cat", False, True, True),
        ("\x01user: catapult", "cat", False, False, True),
        ("\x01user: Cat", "cat", True, False, False),
        ("\x01user: Cat", "cat", False, False, True),
        ("\x01user: black cat", "black cat", False, True, True),  # 多字關鍵字用子字串
        ("\x01user: Item42", "/item\\d+/i", False, True, True),  # regex 對原文
        ("\x01user: Item42", "/item\\d+/", False, True, False),
    ],
)
def test_match_key(hay, key, cs, ww, expected):
    assert _match_key(hay, key, cs, ww) is expected


@pytest.mark.parametrize(
    "logic, secondary, expected",
    [
        (AND_ANY, ["貓", "狗"], True),
        (AND_ANY, ["狗"], False),
        (NOT_ALL, ["貓", "狗"], True),
        (NOT_ALL, ["貓"], False),
        (NOT_ANY, ["狗"], True),
        (NOT_ANY, ["貓"], False),
        (AND_ALL, ["貓", "魚"], True),
        (AND_ALL, ["貓", "狗"], False),
    ],
)
def test_selective_logic(logic, secondary, expected):
    e = entry(1, keys=["你好"], secondary_keys=secondary, selective=True, selective_logic=logic)
    assert (ids(run([e], chat=("使用者: 你好，貓吃魚",))) == [1]) is expected


def test_secondary_ignored_when_not_selective():
    e = entry(1, keys=["你好"], secondary_keys=["狗"], selective=False, selective_logic=AND_ANY)
    assert ids(run([e])) == [1]


def test_constant_disabled_and_no_keys():
    res = run([entry(1, constant=True), entry(2, enabled=False, constant=True), entry(3)])
    assert ids(res) == [1]


def test_scan_depth_limits_messages_and_zero_disables():
    chat = ("使用者: 最新", "角色: 第二", "使用者: 很舊的龍")
    assert ids(run([entry(1, keys=["龍"])], chat=chat)) == []
    assert ids(run([entry(1, keys=["龍"], scan_depth=3)], chat=chat)) == [1]
    assert ids(run([entry(1, keys=["最新"], scan_depth=0)], chat=chat)) == []


def test_probability_zero_never_and_constant_still_rolls():
    assert ids(run([entry(1, constant=True, probability=0, use_probability=True)])) == []
    assert ids(run([entry(1, constant=True, probability=0, use_probability=False)])) == [1]


def test_recursion_and_prevent_recursion():
    a = entry(1, "提到了寶劍", keys=["你好"])
    b = entry(2, "寶劍的設定", keys=["寶劍"])
    assert ids(run([a, b])) and set(ids(run([a, b]))) == {1, 2}
    assert ids(run([a, b], recursive=False)) == [1]
    a.prevent_recursion = True
    assert ids(run([a, b])) == [1]


def test_delay_until_recursion_only_fires_in_recursion():
    a = entry(1, "寶劍", keys=["你好"])
    b = entry(2, keys=["你好"], delay_until_recursion=1)
    assert set(ids(run([a, b]))) == {1, 2}
    a.prevent_recursion = True
    # ST 怪癖：第 1 層在開始時就預設並移出待處理清單；第一輪沒有可遞迴內容時，
    # 不會進入遞迴，delay_until_recursion=1 的條目永遠不觸發（WI:4642-4647、5016-5020）
    assert ids(run([a, b])) == [1]


def test_output_ascending_order_and_positions():
    res = run([
        entry(1, "高", constant=True, order=90),
        entry(2, "低", constant=True, order=10),
        entry(3, "後", constant=True, order=50, position=POS_AFTER),
        entry(4, "深", constant=True, position=POS_DEPTH, depth=2, role=1),
        entry(5, "出口", constant=True, position=POS_OUTLET, outlet_name="衣著"),
    ])
    assert res.before == "低\n高"
    assert res.after == "後"
    assert res.depth == [(2, "user", "深")]
    assert res.outlets == {"衣著": "出口"}


def test_budget_overflow_stops_further_entries():
    big = "字" * 400
    res = activate(
        Lorebook(entries=[entry(1, big, constant=True, order=90), entry(2, big, constant=True, order=10)]),
        ["使用者: 你好"], sources={}, settings=WISettings(budget_percent=1), max_prompt_tokens=50000,
        expand=lambda t: t, rng=random.Random(1), chat_length=1,
    )
    assert ids(res) == [1] and res.warnings


def test_inclusion_group_override_wins():
    res = run([
        entry(1, constant=True, group="衣服", order=10),
        entry(2, constant=True, group="衣服", order=50, group_override=True),
        entry(3, constant=True, group="衣服", order=99),
    ])
    assert ids(res) == [2]


def test_group_scoring_keeps_best_match():
    res = run([
        entry(1, keys=["貓"], group="g", use_group_scoring=True),
        entry(2, keys=["貓", "魚"], group="g", use_group_scoring=True),
    ], chat=("使用者: 貓吃魚",))
    assert ids(res) == [2]


def test_match_sources_extend_scan():
    e = entry(1, keys=["魔法師"], match_sources=["character_description"])
    res = activate(
        Lorebook(entries=[e]), ["使用者: 你好"], sources={"character_description": "她是魔法師"},
        settings=WISettings(), max_prompt_tokens=50000, expand=lambda t: t, rng=random.Random(1), chat_length=1,
    )
    assert ids(res) == [1]


def test_decorators():
    assert ids(run([entry(1, "@@activate\n內容")])) == [1]
    res = run([entry(1, "@@dont_activate\n內容", constant=True)])
    assert ids(res) == []
    assert run([entry(1, "@@activate\n真正內容")]).before == "真正內容"
