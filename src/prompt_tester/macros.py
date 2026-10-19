"""SillyTavern 巨集子集。

語意依 ST 1.18.0 新巨集引擎（public/scripts/macros/）：
- 由左至右解析一次；參數中的巢狀巨集先展開（`if` 例外：延遲展開，只展開選中的分支）
- 巨集輸出不再重新掃描；不認得的巨集原樣保留
- 參數寫法：`{{name::a::b}}`、`{{name: a}}`、`{{name a}}`
- 範圍寫法：`{{if cond}}…{{else}}…{{/if}}`、`{{trim}}…{{/trim}}`、`{{setvar::x}}…{{/setvar}}`
- 後處理：`\\{`/`\\}` 還原、`{{trim}}` 連同前後換行刪除、殘留 else 標記清除

隨機類巨集使用 MacroContext 內的亂數產生器，給定相同 seed 結果可重現。
"""

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime

ELSE_MARKER = "\x00ELSE\x00"
TRIM_MARKER = "{{trim}}"

_IDENT = re.compile(r"[A-Za-z_][\w-]*")
_FLAGS = "!?~#>"
_FALSY = {"", "false", "off", "0"}
# 可用範圍寫法（{{x}}…{{/x}}）的巨集，以及開啟範圍時應有的參數數量
_SCOPED_ARITY = {"if": 1, "trim": 0, "setvar": 1, "//": None, "comment": None}


@dataclass
class MacroContext:
    names: dict[str, str] = field(default_factory=dict)  # char、user 及其他具名值
    variables: dict[str, str] = field(default_factory=dict)  # setvar / getvar（相當於 chat_metadata.variables）
    seed: int = 0
    now: datetime = field(default_factory=datetime.now)
    last_message: str = ""
    last_user_message: str = ""
    last_char_message: str = ""
    original: str = ""
    max_context: int = 0
    max_response: int = 0
    warnings: list[str] = field(default_factory=list)
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)


def evaluate(text: str, ctx: MacroContext) -> str:
    if "{{" not in text and "<" not in text and "\\" not in text:
        return text
    text = _preprocess(text)
    out = _Evaluator(text, ctx).run()
    return _postprocess(out)


# ---- 前 / 後處理 ----

def _preprocess(text: str) -> str:
    text = re.sub(r"\{\{time_(UTC[+-]\d+)\}\}", r"{{time::\1}}", text, flags=re.I)
    text = re.sub(r"<USER>", "{{user}}", text, flags=re.I)
    text = re.sub(r"<(?:BOT|CHAR)>", "{{char}}", text, flags=re.I)
    return text


def _postprocess(text: str) -> str:
    text = re.sub(r"\\([{}])", r"\1", text)
    text = re.sub(r"(?:\r?\n)*\{\{trim\}\}(?:\r?\n)*", "", text, flags=re.I)
    return text.replace(ELSE_MARKER, "")


# ---- 解析 ----

@dataclass
class _Macro:
    name: str  # 小寫
    flags: str
    args: list[str]  # 原始文字，尚未展開
    closing: bool
    start: int
    end: int  # 結尾 `}}` 之後的位置


def _find_macro_end(s: str, i: int) -> int:
    """從 `{{` 之後的位置 i 起，找到對應 `}}` 的起點；考慮巢狀。找不到回傳 -1。"""
    depth = 0
    while i < len(s) - 1:
        pair = s[i : i + 2]
        if s[i] == "\\" :
            i += 2
            continue
        if pair == "{{":
            depth += 1
            i += 2
        elif pair == "}}":
            if depth == 0:
                return i
            depth -= 1
            i += 2
        else:
            i += 1
    return -1


def _split_top_level(s: str, sep: str = "::") -> list[str]:
    parts, depth, last, i = [], 0, 0, 0
    while i < len(s):
        if s.startswith("{{", i):
            depth += 1
            i += 2
        elif s.startswith("}}", i):
            depth -= 1
            i += 2
        elif depth == 0 and s.startswith(sep, i):
            parts.append(s[last:i])
            i += len(sep)
            last = i
        else:
            i += 1
    parts.append(s[last:])
    return parts


def _parse_macro(s: str, start: int) -> _Macro | None:
    body_start = start + 2
    end = _find_macro_end(s, body_start)
    if end < 0:
        return None
    body = s[body_start:end].strip()

    if body.startswith("//"):
        return _Macro("//", "", [body[2:]], False, start, end + 2)

    closing = body.startswith("/")
    if closing:
        body = body[1:].lstrip()
    flags = ""
    while body and body[0] in _FLAGS:
        flags += body[0]
        body = body[1:].lstrip()

    m = _IDENT.match(body)
    if not m:
        return None
    name = m.group(0)
    rest = body[m.end() :]

    if not rest:
        args: list[str] = []
    elif rest.startswith("::"):
        args = _split_top_level(rest[2:])
    elif rest.startswith(":"):
        args = _split_top_level(rest[1:])
    elif rest[0].isspace():
        args = _split_top_level(rest.strip())
    else:
        return None  # 例如 `{{a-b.c}}`：不是合法巨集，視為文字

    args = [a.strip() for a in args]
    return _Macro(name.lower(), flags, args, closing, start, end + 2)


def _opens_scope(mac: _Macro) -> bool:
    arity = _SCOPED_ARITY.get(mac.name, -1)
    return not mac.closing and arity != -1 and (arity is None or len(mac.args) == arity)


def _find_scope_close(s: str, mac: _Macro) -> tuple[int, int] | None:
    """找到與 mac 對應的 `{{/name}}`，回傳 (內容結束位置, close 結束位置)。"""
    depth = 0
    i = mac.end
    while True:
        j = s.find("{{", i)
        if j < 0:
            return None
        inner = _parse_macro(s, j)
        if inner is None:
            i = j + 2
            continue
        if inner.name == mac.name:
            if inner.closing:
                if depth == 0:
                    return j, inner.end
                depth -= 1
            elif _opens_scope(inner):
                depth += 1
        i = inner.end


def _dedent_trim(content: str) -> str:
    """ST trimScopedContent：去掉共同縮排後 trim。"""
    lines = content.split("\n")
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    if not indents:
        return content.strip()
    base = min(indents)
    return "\n".join(l[base:] if len(l) - len(l.lstrip()) >= base else l.lstrip() for l in lines).strip()


def _split_on_else(content: str) -> tuple[str, str | None]:
    depth = 0
    i = 0
    while True:
        j = content.find("{{", i)
        if j < 0:
            return content, None
        mac = _parse_macro(content, j)
        if mac is None:
            i = j + 2
            continue
        if mac.name == "if" and mac.closing:
            depth -= 1
        elif mac.name == "if" and len(mac.args) == 1:
            depth += 1
        elif mac.name == "else" and depth == 0:
            return content[:j], content[mac.end :]
        i = mac.end


# ---- 求值 ----

class _Evaluator:
    def __init__(self, source: str, ctx: MacroContext):
        self.source = source
        self.ctx = ctx
        self.content_hash = hashlib.sha256(source.encode()).hexdigest()[:16]

    def run(self) -> str:
        return self.resolve(self.source, 0)

    def resolve(self, s: str, offset: int) -> str:
        out: list[str] = []
        i = 0
        while i < len(s):
            j = s.find("{{", i)
            if j < 0:
                out.append(s[i:])
                break
            # `\{{` 為跳脫，保留給後處理還原
            if j > 0 and s[j - 1] == "\\":
                out.append(s[i : j + 1])
                i = j + 1
                continue
            out.append(s[i:j])
            mac = _parse_macro(s, j)
            if mac is None or mac.closing:
                out.append("{{")
                i = j + 2
                continue

            scoped: str | None = None
            if _opens_scope(mac):
                close = _find_scope_close(s, mac)
                if close is not None:
                    scoped = s[mac.end : close[0]]
                    mac.end = close[1]

            out.append(self.call(mac, scoped, s[j : mac.end], offset + j))
            i = mac.end
        return "".join(out)

    def call(self, mac: _Macro, scoped: str | None, raw: str, offset: int) -> str:
        name = mac.name
        if name in ("//", "comment"):
            return ""
        if name == "if":
            return self._if(mac, scoped, offset)

        args = [self.resolve(a, offset) for a in mac.args]
        if scoped is not None:
            args.append(_dedent_trim(self.resolve(scoped, offset)))

        handler = _HANDLERS.get(name)
        if handler is None:
            if name in self.ctx.names:
                return self.ctx.names[name]
            self.ctx.warnings.append(f"不支援的巨集：{raw[:60]}")
            return raw
        return handler(self, args, scoped is not None, offset)

    def _if(self, mac: _Macro, scoped: str | None, offset: int) -> str:
        if not mac.args:
            return ""
        raw_cond = mac.args[0]
        raw_content = scoped if scoped is not None else (mac.args[1] if len(mac.args) > 1 else "")

        inverted = raw_cond.lstrip().startswith("!") or "!" in mac.flags
        cond = self.resolve(raw_cond.lstrip().lstrip("!").strip(), offset)
        if cond in self.ctx.names or cond.lower() in _NO_ARG_MACROS:
            cond = self.resolve("{{" + cond + "}}", offset)

        falsy = cond.strip().lower() in _FALSY
        if inverted:
            falsy = not falsy

        then_branch, else_branch = _split_on_else(raw_content)
        chosen = else_branch if falsy else then_branch
        if chosen is None:
            return ""
        result = self.resolve(chosen, offset)
        return result if "#" in mac.flags else _dedent_trim(result)

    # -- 隨機 --

    def pick_rng(self, offset: int) -> random.Random:
        key = f"{self.ctx.seed}-{self.content_hash}-{offset}"
        return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def _read_list(args: list[str]) -> list[str]:
    if len(args) != 1:
        return args
    s = args[0]
    if "::" in s:
        return [x.strip() for x in s.split("::")]
    s = s.replace("\\,", "\x00")
    return [x.strip().replace("\x00", ",") for x in s.split(",")]


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def _fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


_ROLL = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.I)


def _roll(ev: _Evaluator, args, scoped, offset) -> str:
    formula = args[0] if args else ""
    if formula.isdigit():
        formula = f"1d{formula}"
    m = _ROLL.match(formula)
    if not m:
        ev.ctx.warnings.append(f"無效的骰子公式：{formula}")
        return ""
    count, sides = int(m.group(1) or 1), int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    return str(sum(ev.ctx.rng.randint(1, sides) for _ in range(count)) + mod)


def _random(ev: _Evaluator, args, scoped, offset) -> str:
    items = _read_list(args)
    return ev.ctx.rng.choice(items) if items else ""


def _pick(ev: _Evaluator, args, scoped, offset) -> str:
    items = _read_list(args)
    return ev.pick_rng(offset).choice(items) if items else ""


def _setvar(ev: _Evaluator, args, scoped, offset) -> str:
    if args:
        ev.ctx.variables[args[0]] = args[1] if len(args) > 1 else ""
    return ""


def _getvar(ev: _Evaluator, args, scoped, offset) -> str:
    return ev.ctx.variables.get(args[0], "") if args else ""


def _addvar(ev: _Evaluator, args, scoped, offset) -> str:
    if len(args) < 2:
        return ""
    name, value = args[0], args[1]
    current = ev.ctx.variables.get(name, "")
    a, b = _num(current or "0"), _num(value)
    if a is not None and b is not None:
        ev.ctx.variables[name] = _fmt_num(a + b)
    else:
        # ST addLocalVariable：String(currentValue || '') + value，數值 0 視為空字串
        ev.ctx.variables[name] = ("" if a == 0 else current) + value
    return ""


def _step_var(delta: int):
    def handler(ev: _Evaluator, args, scoped, offset) -> str:
        if not args:
            return ""
        current = _num(ev.ctx.variables.get(args[0], "0") or "0") or 0
        ev.ctx.variables[args[0]] = _fmt_num(current + delta)
        return ev.ctx.variables[args[0]]

    return handler


def _hasvar(ev: _Evaluator, args, scoped, offset) -> str:
    return "true" if args and args[0] in ev.ctx.variables else "false"


def _deletevar(ev: _Evaluator, args, scoped, offset) -> str:
    if args:
        ev.ctx.variables.pop(args[0], None)
    return ""


def _time(ev: _Evaluator, args, scoped, offset) -> str:
    now = ev.ctx.now
    if args and (m := re.fullmatch(r"UTC([+-]\d+)", args[0], re.I)):
        from datetime import timedelta, timezone

        now = now.astimezone(timezone(timedelta(hours=int(m.group(1)))))
    return now.strftime("%I:%M %p").lstrip("0")


def _trim(ev: _Evaluator, args, scoped, offset) -> str:
    return args[-1] if scoped and args else TRIM_MARKER


def _count_arg(args: list[str]) -> int:
    n = _num(args[0]) if args else None
    return int(n) if n else 1


_HANDLERS = {
    "noop": lambda ev, a, s, o: "",
    "trim": _trim,
    "newline": lambda ev, a, s, o: "\n" * _count_arg(a),
    "space": lambda ev, a, s, o: " " * _count_arg(a),
    "else": lambda ev, a, s, o: ELSE_MARKER,
    "reverse": lambda ev, a, s, o: a[0][::-1] if a else "",
    "random": _random,
    "pick": _pick,
    "roll": _roll,
    "setvar": _setvar,
    "getvar": _getvar,
    "addvar": _addvar,
    "incvar": _step_var(1),
    "decvar": _step_var(-1),
    "hasvar": _hasvar,
    "deletevar": _deletevar,
    "time": _time,
    "date": lambda ev, a, s, o: f"{ev.ctx.now:%B} {ev.ctx.now.day}, {ev.ctx.now.year}",
    "weekday": lambda ev, a, s, o: f"{ev.ctx.now:%A}",
    "isotime": lambda ev, a, s, o: f"{ev.ctx.now:%H:%M}",
    "isodate": lambda ev, a, s, o: f"{ev.ctx.now:%Y-%m-%d}",
    "lastmessage": lambda ev, a, s, o: ev.ctx.last_message,
    "lastusermessage": lambda ev, a, s, o: ev.ctx.last_user_message,
    "lastcharmessage": lambda ev, a, s, o: ev.ctx.last_char_message,
    "original": lambda ev, a, s, o: ev.ctx.original,
    "maxprompt": lambda ev, a, s, o: str(ev.ctx.max_context - ev.ctx.max_response),
    "maxcontext": lambda ev, a, s, o: str(ev.ctx.max_context),
    "maxresponse": lambda ev, a, s, o: str(ev.ctx.max_response),
}

# 可當作 {{if 名稱}} 條件直接解析的無參數巨集
_NO_ARG_MACROS = {
    "lastmessage",
    "lastusermessage",
    "lastcharmessage",
    "original",
    "isotime",
    "isodate",
    "weekday",
    "date",
    "time",
}

SUPPORTED_MACROS = sorted({"char", "user", "//", "comment", "if", *_HANDLERS})
