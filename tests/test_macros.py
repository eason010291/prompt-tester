from datetime import datetime

import pytest

from prompt_tester.macros import MacroContext, evaluate


def ev(text: str, **kw) -> str:
    kw.setdefault("names", {"char": "聿", "user": "藍"})
    return evaluate(text, MacroContext(**kw))


@pytest.mark.parametrize(
    "src, out",
    [
        ("{{char}} 與 {{user}}", "聿 與 藍"),
        ("<USER> 和 <BOT>", "藍 和 聿"),
        ("a{{// 註解 {{char}} }}b", "ab"),
        ("a{{comment::x}}b", "ab"),
        ("a{{noop}}b", "ab"),
        ("x{{newline}}y", "x\ny"),
        ("x{{newline::2}}y", "x\n\ny"),
        ("{{reverse::abc}}", "cba"),
        ("沒有巨集", "沒有巨集"),
    ],
)
def test_basic(src, out):
    assert ev(src) == out


def test_unknown_macro_kept_literal_and_warned():
    ctx = MacroContext(names={"char": "聿"})
    assert evaluate("{{foo::bar}} {{char}}", ctx) == "{{foo::bar}} 聿"
    assert any("foo" in w for w in ctx.warnings)


def test_output_is_not_rescanned():
    ctx = MacroContext(variables={"x": "{{char}}"}, names={"char": "聿"})
    assert evaluate("{{getvar::x}}", ctx) == "{{char}}"


def test_nested_args_resolve_inner_first():
    ctx = MacroContext(names={"char": "聿"})
    evaluate("{{setvar::name::{{char}}}}", ctx)
    assert ctx.variables["name"] == "聿"


def test_escaped_braces():
    assert ev(r"\{\{char\}\}") == "{{char}}"


def test_legacy_trim_removes_surrounding_newlines():
    assert ev("a\n\n{{trim}}\n\nb") == "ab"


def test_scoped_trim():
    assert ev("[{{trim}}\n   hi\n{{/trim}}]") == "[hi]"


@pytest.mark.parametrize(
    "src, out",
    [
        ("{{if 1}}yes{{/if}}", "yes"),
        ("{{if 0}}yes{{/if}}", ""),
        ("{{if off}}yes{{else}}no{{/if}}", "no"),
        ("{{if !0}}yes{{/if}}", "yes"),
        ("{{if char}}有角色{{/if}}", "有角色"),
        ("{{if {{getvar::flag}}}}on{{else}}off{{/if}}", "off"),
        ("{{if 1::inline}}", "inline"),
        ("{{if 1}}a{{if 0}}b{{else}}c{{/if}}d{{else}}e{{/if}}", "acd"),
        ("{{if 1}}\n    縮排內容\n{{/if}}", "縮排內容"),
    ],
)
def test_if(src, out):
    assert ev(src) == out


def test_if_only_evaluates_chosen_branch():
    ctx = MacroContext()
    evaluate("{{if 0}}{{setvar::a::1}}{{else}}{{setvar::b::2}}{{/if}}", ctx)
    assert ctx.variables == {"b": "2"}


def test_variables():
    ctx = MacroContext()
    out = evaluate(
        "{{setvar::n::5}}{{addvar::n::3}}{{incvar::n}}|{{getvar::n}}|{{hasvar::n}}"
        "{{deletevar::n}}|{{hasvar::n}}{{addvar::s::ab}}{{addvar::s::cd}}|{{getvar::s}}",
        ctx,
    )
    assert out == "9|9|true|false|abcd"


def test_scoped_setvar():
    ctx = MacroContext()
    evaluate("{{setvar::bio}}\n  多行\n  內容\n{{/setvar}}", ctx)
    assert ctx.variables["bio"] == "多行\n內容"


def test_random_reproducible_with_seed():
    src = "{{random::a::b::c::d::e::f}}{{random::a::b::c::d::e::f}}{{roll::1d100}}"
    assert ev(src, seed=7) == ev(src, seed=7)
    assert len({ev(src, seed=s) for s in range(20)}) > 1


def test_random_comma_list():
    assert ev("{{random::x}}") == "x"
    assert ev(r"{{random a\,b}}") == "a,b"
    assert ev("{{random: a, b}}") in ("a", "b")


def test_pick_stable_per_position():
    src = "{{pick::a::b::c::d::e::f::g::h}}"
    assert ev(src, seed=1) == ev(src, seed=1)


@pytest.mark.parametrize("formula", ["6", "1d6", "2d6+3", "3d4-1"])
def test_roll(formula):
    assert ev(f"{{{{roll::{formula}}}}}").lstrip("-").isdigit()


def test_roll_invalid_warns():
    ctx = MacroContext()
    assert evaluate("{{roll::abc}}", ctx) == ""
    assert ctx.warnings


def test_time_macros_use_context_now():
    now = datetime(2026, 10, 19, 15, 4)
    assert ev("{{isodate}} {{isotime}} {{weekday}}", now=now) == "2026-10-19 15:04 Monday"
    assert ev("{{date}}", now=now) == "October 19, 2026"
    assert ev("{{time}}", now=now) == "3:04 PM"


def test_unclosed_braces_left_as_text():
    assert ev("{{char") == "{{char"
    assert ev("{{ }}") == "{{ }}"
