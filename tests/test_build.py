from prompt_tester.build import build
from prompt_tester.preset import POSITION_IN_CHAT, Block, HistoryMessage, Options, Params, Preset

NAMES = {"char": "聿", "user": "藍"}


def blk(name, content="", **kw) -> Block:
    return Block(id=name, name=name, content=content or name, **kw)


HISTORY_MARKER = Block(id="hist", name="聊天歷史", kind="marker", marker="chatHistory")


def h(role, content):
    return HistoryMessage(role=role, content=content)


def simple(result):
    return [(m.role, m.content) for m in result.messages]


def preset(*blocks, squash=False, post="", new_chat="[Start]", **params) -> Preset:
    return Preset(
        blocks=list(blocks),
        options=Options(squash_system_messages=squash, post_processing=post, new_chat_prompt=new_chat),
        params=Params(**params) if params else Params(),
    )


def test_order_follows_block_list_including_post_history():
    p = preset(blk("main"), HISTORY_MARKER, blk("after", role="user"))
    r = build(p, [h("user", "hi")], NAMES)
    assert simple(r) == [("system", "main"), ("system", "[Start]"), ("user", "hi"), ("user", "after")]


def test_disabled_and_divider_blocks_skipped():
    p = preset(
        blk("===分隔===", kind="divider", enabled=False),
        blk("off", enabled=False),
        blk("on"),
    )
    assert simple(build(p, [], NAMES)) == [("system", "on")]


def test_no_history_marker_means_no_history_and_no_injections():
    p = preset(blk("main"), blk("inj", injection_position=POSITION_IN_CHAT, injection_depth=0))
    r = build(p, [h("user", "hi")], NAMES)
    assert simple(r) == [("system", "main")]
    assert r.warnings


def test_macros_expanded_in_blocks_and_history():
    p = preset(blk("main", "我是{{char}}"), HISTORY_MARKER)
    r = build(p, [h("user", "{{user}}來了")], NAMES)
    assert simple(r)[0] == ("system", "我是聿")
    assert simple(r)[-1] == ("user", "藍來了")


def test_expand_macros_false_keeps_raw():
    p = preset(blk("main", "我是{{char}}"))
    assert simple(build(p, [], NAMES, expand_macros=False)) == [("system", "我是{{char}}")]


def test_empty_new_chat_prompt_dropped():
    p = preset(HISTORY_MARKER, new_chat="")
    assert simple(build(p, [h("user", "hi")], NAMES)) == [("user", "hi")]


def test_depth_zero_injects_after_last_message():
    p = preset(HISTORY_MARKER, blk("inj", "注入", injection_position=POSITION_IN_CHAT, injection_depth=0), new_chat="")
    r = build(p, [h("user", "a"), h("assistant", "b")], NAMES)
    assert simple(r) == [("user", "a"), ("assistant", "b"), ("system", "注入")]


def test_depth_counts_from_end_and_overflow_goes_to_top():
    p = preset(
        HISTORY_MARKER,
        blk("d1", "d1", injection_position=POSITION_IN_CHAT, injection_depth=1),
        blk("d9", "d9", injection_position=POSITION_IN_CHAT, injection_depth=9),
        new_chat="",
    )
    r = build(p, [h("user", "a"), h("assistant", "b")], NAMES)
    assert simple(r) == [("system", "d9"), ("user", "a"), ("system", "d1"), ("assistant", "b")]


def test_same_depth_order_and_role_sequence_matches_st():
    """同深度：order 小的在前；同 order 內實際順序 assistant → user → system（ST 程式碼行為）。"""
    inj = dict(injection_position=POSITION_IN_CHAT, injection_depth=0)
    p = preset(
        HISTORY_MARKER,
        blk("s100", "s100", role="system", **inj),
        blk("u100", "u100", role="user", **inj),
        blk("a100", "a100", role="assistant", **inj),
        blk("s50", "s50", role="system", injection_order=50, **inj),
        blk("s100b", "s100b", role="system", **inj),
        new_chat="",
    )
    r = build(p, [h("user", "a")], NAMES)
    assert simple(r) == [
        ("user", "a"),
        ("system", "s50"),
        ("assistant", "a100"),
        ("user", "u100"),
        ("system", "s100\ns100b"),
    ]


def test_squash_merges_consecutive_system_but_not_separator():
    p = preset(blk("a"), blk("b"), HISTORY_MARKER, blk("c"), blk("d", role="user"), squash=True)
    r = build(p, [], NAMES)
    assert simple(r) == [("system", "a\nb"), ("system", "[Start]"), ("system", "c"), ("user", "d")]
    assert [s.block_id for s in r.messages[0].sources] == ["a", "b"]


def test_budget_trims_oldest_history_first():
    long = "字" * 200
    p = preset(blk("main"), HISTORY_MARKER, max_context=600, max_tokens=100)
    hist = [h("user", f"{i}{long}") for i in range(5)]
    r = build(p, hist, NAMES)
    assert 0 < r.history_included < 5
    kept = [m.content for m in r.messages if m.sources[0].origin == "history"]
    assert kept == [x.content for x in hist[-r.history_included :]]
    assert any("預算不足" in w for w in r.warnings)


def test_post_processing_merge():
    p = preset(blk("a"), blk("b"), blk("u", role="user"), blk("v", role="user"), post="merge")
    assert simple(build(p, [], NAMES)) == [("system", "a\n\nb"), ("user", "u\n\nv")]


def test_post_processing_semi_turns_later_system_into_user():
    p = preset(blk("a"), blk("u", role="user"), blk("b"), post="semi")
    assert simple(build(p, [], NAMES)) == [("system", "a"), ("user", "u\n\nb")]


def test_post_processing_strict_adds_placeholder():
    p = preset(blk("a"), blk("x", role="assistant"), post="strict")
    p.options.prompt_placeholder = "[PH]"
    assert simple(build(p, [], NAMES)) == [("system", "a"), ("user", "[PH]"), ("assistant", "x")]


def test_post_processing_single():
    p = preset(blk("a"), blk("x", role="assistant"), post="single")
    assert simple(build(p, [], NAMES)) == [("user", "a\n\nx")]


def test_variables_persist_across_blocks_and_history():
    p = preset(blk("set", "{{setvar::n::1}}"), blk("inc", "{{incvar::n}}"), HISTORY_MARKER, new_chat="")
    r = build(p, [h("user", "n={{getvar::n}}")], NAMES)
    assert simple(r) == [("system", "2"), ("user", "n=2")]
    assert r.variables == {"n": "2"}


def test_block_tokens_reported_for_enabled_blocks():
    r = build(preset(blk("a", "hello"), blk("off", enabled=False)), [], NAMES)
    assert set(r.block_tokens) == {"a"} and r.block_tokens["a"] > 0
