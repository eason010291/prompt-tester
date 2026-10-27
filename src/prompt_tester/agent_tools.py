"""修改者 LLM 的工具（design.md §7.3）：單一來源的定義，供 ① 內建迴圈 ② MCP ③ REST 共用。

- 一個頂部分頁一支工具：prompt（提示詞）、scenario（情境）、experiment（實驗）、script（腳本）、versions（版本）。
  每支工具以 action 區分動作，參數扁平；預設與情境的欄位統一用路徑定位（doc_paths）。
- 權限按「工具 + 動作 + 路徑」在伺服器端檢查：內建迴圈一輪只開放一個修改對象；花錢的動作只給外部 agent 且需開啟。
- 預設的提交在受保護的分支（main）上時自動建立新分支；情境的修改以線性版本保存，可 diff、可還原。
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from . import doc_paths as dp
from . import experiments, feedback, metrics, store, vcs, workspace
from . import scenario_revisions as revisions
from .entities import Lorebook, Scenario
from .preset import POSITION_IN_CHAT, Block, Preset
from .routes.prompt import build_for, get_workspace, resolve_scenario

AGENT_KEY = "agent"
MAX_RESULT_CHARS = 16000

DEFAULT_SYSTEM_PROMPT = """你是 SillyTavern 角色扮演提示詞（Chat Completion 預設）與角色卡的修改者。使用者用這個工具測試，對生成結果寫了評語；你的工作是依評語修改，讓下一輪生成更好。

工具：prompt（預設）、scenario（情境與角色卡）、experiment（評語與回覆）。每支工具用 action 指定動作，欄位用 path 定位。

工作方式：
1. 先讀評語與被評的回覆，找出問題的根本原因在哪裡。必要時用 read 讀全文、用 prompt 的 preview 看組裝後實際送出的內容。
2. 做「最小、有針對性」的修改：優先用 patch 局部替換；只有需要整段重寫時才用 set。不要順手改寫沒被提到的地方。
3. 保留既有結構與慣例：巨集（{{char}}、{{user}}、{{getvar::…}} 等）、XML / HTML 標籤與狀態欄格式（使用者的正則依賴這些標籤）、原文的語言與語氣。marker 區塊（聊天歷史、角色描述等）沒有內容，不要動。
4. 評語彼此矛盾或資訊不足時，選擇保守的改法，並在提交說明中寫出取捨。
5. 改完後呼叫一次 commit，說明要條列：改了哪些地方、各自針對哪條評語、預期效果。提交後就結束，不需要再回覆。

注意：分數是使用者自訂的「屬性：n/10」，低分的屬性通常是重點。"""

# 舊版的預設系統提示詞（工具名稱已改）：設定裡存的還是這些時，自動換成新版
_LEGACY_PROMPTS = [
    """你是 SillyTavern 角色扮演提示詞（Chat Completion 預設）的修改者。使用者用這個工具測試預設，對生成結果寫了評語；你的工作是依評語修改預設，讓下一輪生成更好。

工作方式：
1. 先讀評語與被評的回覆，找出問題的根本原因屬於哪個區塊。必要時用 get_block 讀全文、用 preview 看組裝後實際送出的內容。
2. 做「最小、有針對性」的修改：優先用 patch_block 局部替換；只有需要整段重寫時才用 edit_block。不要順手改寫沒被提到的地方。
3. 保留預設的既有結構與慣例：巨集（{{char}}、{{user}}、{{getvar::…}} 等）、XML / HTML 標籤與狀態欄格式（使用者的正則依賴這些標籤）、區塊的語言與語氣。marker 區塊（聊天歷史、角色描述等）沒有內容，不要動。
4. 評語彼此矛盾或資訊不足時，選擇保守的改法，並在提交說明中寫出取捨。
5. 改完後呼叫 commit 一次，說明要條列：改了哪些區塊、各自針對哪條評語、預期效果。提交後就結束，不需要再回覆。

注意：分數是使用者自訂的「屬性：n/10」，低分的屬性通常是重點。""",
]


class AgentSettings(BaseModel):
    """修改者設定（kv：agent）。"""

    connection_id: int | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # 舊版系統提示詞（最新在前），可還原
    prompt_history: list[dict] = Field(default_factory=list)
    temperature: float = 0.3
    max_tokens: int = 16000
    # 內建迴圈：最多幾次 LLM 呼叫、幾次工具呼叫、花費上限（USD）
    max_steps: int = 24
    max_tool_calls: int = 60
    budget_usd: float = 0.5
    # 是否允許修改取樣參數
    allow_params: bool = True
    # 是否允許外部 agent 執行實驗（會產生 LLM 費用）
    allow_run_experiment: bool = False
    # 是否允許修改情境的測試條件（既有歷史、逐回合使用者輸入、人設、目標、變數）；修改會標在版本紀錄
    allow_fixtures: bool = True


def load_settings(conn: sqlite3.Connection) -> AgentSettings:
    s = AgentSettings.model_validate(store.get_setting(conn, AGENT_KEY) or {})
    if s.system_prompt in _LEGACY_PROMPTS:
        s.system_prompt = DEFAULT_SYSTEM_PROMPT
    return s


def save_settings(conn: sqlite3.Connection, s: AgentSettings) -> AgentSettings:
    old = load_settings(conn)
    if old.system_prompt != s.system_prompt and old.system_prompt.strip():
        # 系統提示詞變更時保留舊版（最多 20 份）
        s.prompt_history = [{"text": old.system_prompt, "saved_at": store.now_iso()}, *old.prompt_history][:20]
    store.set_setting(conn, AGENT_KEY, s.model_dump())
    return s


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    conn: sqlite3.Connection
    author: str  # llm:<model> / agent:<客戶端>
    settings: AgentSettings
    manager: "experiments.Manager | None" = None
    # 內建迴圈的提交附帶資訊
    meta: dict = field(default_factory=dict)
    branch_prefix: str = "llm"
    changed: bool = False
    committed: dict | None = None
    # 內建迴圈修改角色卡時，只能動這個情境
    scenario_id: int | None = None
    # 內建迴圈：允許的 {工具: 動作}；None = 不限制（外部 agent，仍受設定開關約束）
    allowed: dict[str, list[str]] | None = None


EMPTY_HINT = {"string": "；用不到時填空字串", "integer": "；用不到時填 0", "array": "；用不到時填 []"}
# 空值仍有意義、不能當成「沒給」的參數
KEEP_EMPTY = {"replace", "value", "name"}


def _drop_empty(raw: dict) -> dict:
    """schema 要求每個參數都填；用不到的空字串 / 0 / [] / null 視為沒給（index 用 -1）。"""
    out = {}
    for k, v in raw.items():
        if v is None or (k not in KEEP_EMPTY and (v == "" or v == [])):
            continue
        if k == "value" and v == "null":  # 模型常把 null 寫成字串
            continue
        if k == "index" and isinstance(v, int) and v < 0:
            continue
        if k != "index" and k not in KEEP_EMPTY and isinstance(v, int) and not isinstance(v, bool) and v == 0:
            continue
        out[k] = v
    return out


@dataclass
class Action:
    name: str
    doc: str
    fn: Callable[[Ctx, Any], Any]
    required: tuple[str, ...] = ()
    builtin: bool = True  # 內建迴圈可用
    gate: str | None = None  # 需要開啟的設定欄位


@dataclass
class Tool:
    name: str
    summary: str
    args: type[BaseModel]
    guide: str = ""
    actions: dict[str, Action] = field(default_factory=dict)

    def action(self, name: str, doc: str, *, required: tuple[str, ...] = (), builtin: bool = True, gate: str | None = None):
        def deco(fn):
            self.actions[name] = Action(name, doc, fn, required, builtin, gate)
            return fn

        return deco

    def enabled(self, settings: AgentSettings, allowed: list[str] | None = None) -> list[str]:
        return [a.name for a in self.actions.values()
                if (not a.gate or getattr(settings, a.gate)) and (allowed is None or a.name in allowed)]

    def description(self, actions: list[str]) -> str:
        lines = [self.summary, "", "action："]
        lines += [f"- {n}：{self.actions[n].doc}" for n in actions]
        if self.guide:
            lines += ["", self.guide]
        return "\n".join(lines)

    def schema(self, actions: list[str]) -> dict:
        s = self.args.model_json_schema()
        s.pop("title", None)
        for p in s.get("properties", {}).values():
            p.pop("title", None)
            # 選填參數攤平成單一型別：anyOf [X, null] 會讓部分模型（DeepSeek Flash 等）直接略過該參數
            any_of = p.pop("anyOf", None)
            if any_of:
                p.update(next((x for x in any_of if x.get("type") != "null"), {}))
            if p.get("default") is None:
                p.pop("default", None)
        s["properties"]["action"] = {"type": "string", "enum": actions, "description": "要執行的動作"}
        # 全部標為必填（用不到的填空字串 / 0 / null）：選填參數常被較弱的模型略過（實測 DeepSeek V4.1 Flash 會漏掉 replace、path）
        s["required"] = list(s["properties"])
        for name, p in s["properties"].items():
            if name == "index":
                p["description"] = p.get("description", "") + "；用不到時填 -1"
            elif name != "action":
                p["description"] = p.get("description", "") + EMPTY_HINT.get(p.get("type"), "；用不到時填 null")
        return s


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…（共 {len(text)} 字）"


def _validation_text(e: ValidationError) -> str:
    return "；".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors()[:8])


def call(ctx: Ctx, name: str, raw_args: dict | str | None) -> Any:
    """執行工具；錯誤以 ToolError 回報（訊息給 LLM 看，讓它修正後重試）。"""
    t = REGISTRY.get(name)
    if t is None:
        raise ToolError(f"沒有這個工具：{name}（可用：{'、'.join(REGISTRY)}）")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            raise ToolError(f"參數不是合法的 JSON：{e.msg}") from e
    raw_args = raw_args or {}
    if not isinstance(raw_args, dict):
        raise ToolError("參數必須是 JSON 物件")
    raw_args = _drop_empty(raw_args)
    allowed = None if ctx.allowed is None else ctx.allowed.get(name, [])
    usable = t.enabled(ctx.settings, allowed)
    act = t.actions.get(raw_args.get("action") or "")
    if act is None:
        raise ToolError(f"{name} 需要 action，可用：{'、'.join(usable)}")
    if act.gate and not getattr(ctx.settings, act.gate):
        raise ToolError(f"{name}.{act.name} 未開啟（設定頁 → 修改者 LLM）")
    if act.name not in usable:
        raise ToolError(f"這一輪不能使用 {name}.{act.name}（可用：{'、'.join(usable) or '無'}）")
    try:
        args = t.args.model_validate(raw_args)
    except ValidationError as e:
        raise ToolError("參數錯誤：" + _validation_text(e)) from e
    missing = [f for f in act.required if getattr(args, f) is None]
    if missing:
        raise ToolError(f"{name}.{act.name} 需要 {'、'.join(missing)}")
    try:
        return act.fn(ctx, args)
    except ValidationError as e:
        raise ToolError("修改後的內容不合法：" + _validation_text(e)) from e
    except (vcs.VcsError, experiments.ExperimentError, KeyError, ValueError) as e:
        raise ToolError(str(e).strip("'")) from e


def openai_tools(allowed: dict[str, list[str]], settings: AgentSettings) -> list[dict]:
    out = []
    for name, acts in allowed.items():
        t = REGISTRY[name]
        actions = t.enabled(settings, acts)
        if actions:
            out.append({"type": "function", "function": {"name": t.name, "description": t.description(actions),
                                                          "parameters": t.schema(actions)}})
    return out


# 內建迴圈一輪只改一個對象：該對象的工具全開（內建可用的動作），其他只讀
_BUILTIN = {
    "preset": {"prompt": None, "scenario": ["list", "read"], "experiment": ["feedback", "reply"]},
    "card": {"scenario": None, "prompt": ["read", "preview"], "experiment": ["feedback", "reply"]},
}


def builtin_allowed(settings: AgentSettings, target: str = "preset") -> dict[str, list[str]]:
    out = {}
    for name, acts in _BUILTIN[target].items():
        t = REGISTRY[name]
        out[name] = [a for a in t.enabled(settings, acts) if t.actions[a].builtin]
    return out


def to_text(result: Any, limit: int = MAX_RESULT_CHARS) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"…（已截斷，共 {len(text)} 字）"


def _patch(text: Any, find: str, replace: str, where: str) -> str:
    if not isinstance(text, str):
        raise ToolError(f"{where} 不是文字欄位；patch 只能用在文字上（其他型別請用 set）")
    if not find:
        raise ToolError("find 不能是空的")
    n = text.count(find)
    if n == 0:
        raise ToolError(f"find 在 {where} 中找不到；請先 read 原文後逐字複製（含空白與換行）")
    if n > 1:
        raise ToolError(f"find 在 {where} 中出現 {n} 次，請加長片段使其唯一")
    return text.replace(find, replace, 1)


# =====================================================================
# prompt：工作區的預設（提示詞頁）
# =====================================================================

class PromptArgs(BaseModel):
    action: Literal["read", "set", "patch", "add", "remove", "move", "preview", "diff", "commit"]
    path: str | None = Field(None, description="欄位路徑，例如 blocks[<id>].content、params.temperature")
    value: Any = Field(None, description="set：新的值（任何 JSON 型別）；add：新區塊 {name, content, role?, enabled?}")
    find: str | None = Field(None, description="patch：要替換的原文，必須恰好出現一次")
    replace: str | None = Field(None, description="patch：替換成的文字；空字串代表刪除")
    after: str | None = Field(None, description="add / move：放在這個區塊 id 之後；省略時放在最前面")
    scenario_id: int | None = Field(None, description="preview：用哪個情境組裝；省略時用提示詞頁目前選的情境")
    max_chars: int = Field(1500, description="preview：每則訊息最多回傳幾字")
    message: str | None = Field(None, description="commit：提交說明（改了哪些區塊、針對哪些評語、預期效果）")


PROMPT = Tool(
    "prompt",
    "提示詞頁：讀取與修改工作區的預設（區塊、取樣參數），預覽組裝結果，提交成版本。",
    PromptArgs,
    guide=(
        "路徑：blocks[<區塊 id>]（id 可用 6 碼以上前綴）之下有 content、name、enabled、role（system/user/assistant）、"
        "injection_position（0 = 依順序，1 = 插入聊天中）、injection_depth、injection_order；"
        "params.temperature / top_p / frequency_penalty / presence_penalty / max_tokens；options.<名稱>；name。"
        "marker 區塊（聊天歷史、角色描述等）的內容由情境填入，不能改。"
    ),
)

PRESET_SPEC = dp.Spec(
    keyed={("blocks",): "id"},
    prefix_keys={"id"},
    blocked={("blocks", "id"), ("blocks", "kind"), ("blocks", "marker"), ("blocks", "st"), ("blocks", "collapsed"), ("extra",)},
)
PARAM_RANGES = {"temperature": (0, 2), "top_p": (0, 1), "frequency_penalty": (-2, 2), "presence_penalty": (-2, 2),
                "max_tokens": (1, None), "max_context": (1, None)}


def _preset(ctx: Ctx) -> Preset:
    return workspace.working(ctx.conn)


def _summary(b: Block, i: int) -> dict:
    d = {"index": i, "id": b.id, "name": b.name, "role": b.role, "enabled": b.enabled, "chars": len(b.content)}
    if b.kind != "content":
        d["kind"] = b.kind
    if b.marker:
        d["marker"] = b.marker
    if b.injection_position == POSITION_IN_CHAT:
        d["in_chat_depth"] = b.injection_depth
    if b.group:
        d["group"] = b.group
    return d


def _preset_gate(ctx: Ctx):
    def check(names: tuple[str, ...]) -> str | None:
        if names[0] == "params" and not ctx.settings.allow_params:
            return "取樣參數不開放修改（設定頁 → 修改者 LLM → 允許修改取樣參數）"
        return None

    return check


def _save_preset(ctx: Ctx, doc: dict) -> Preset:
    p = Preset.model_validate(doc)
    for k, (lo, hi) in PARAM_RANGES.items():
        v = getattr(p.params, k)
        if v < lo or (hi is not None and v > hi):
            raise ToolError(f"params.{k} 必須在 {lo}～{hi if hi is not None else '∞'} 之間")
    for b in p.blocks:
        if b.injection_depth < 0:
            raise ToolError(f"區塊「{b.name}」的 injection_depth 不能是負數")
    workspace.save_preset(ctx.conn, p)
    ctx.changed = True
    return p


def _locate_block(doc: dict, path: str) -> dp.Loc:
    loc = dp.locate(doc, path, PRESET_SPEC)
    if loc.names != ("blocks",) or not isinstance(loc.parent, list):
        raise ToolError(f"{path} 不是區塊；請用 blocks[<id>]")
    return loc


def _check_marker(loc: dp.Loc) -> None:
    if loc.names == ("blocks", "content") and loc.parent.get("kind") == "marker":
        raise ToolError("marker 區塊的內容由情境填入，不能編輯")


@PROMPT.action("read", "不帶 path：列出所有區塊（順序即組裝順序）、參數、分支與是否有未提交的變更；帶 path：讀該處的值（blocks[<id>] 讀區塊全文與設定）")
def prompt_read(ctx: Ctx, a: PromptArgs) -> Any:
    p = _preset(ctx)
    if not a.path:
        branch, base = workspace.head(ctx.conn)
        return {
            "preset": p.name, "branch": branch, "base_commit": base[:8] if base else None,
            "uncommitted_changes": workspace.is_dirty(ctx.conn), "params": p.params.model_dump(),
            "blocks": [_summary(b, i) for i, b in enumerate(p.blocks)],
            "hint": "這是概覽，不含區塊內容。讀區塊全文：{\"action\": \"read\", \"path\": \"blocks[<id>]\"}",
        }
    loc = dp.locate(p.model_dump(mode="json"), a.path, PRESET_SPEC)
    if loc.names == ("blocks",) and isinstance(loc.parent, list):
        b = p.blocks[loc.key]
        return _summary(b, loc.key) | {"content": b.content, "injection_position": b.injection_position,
                                       "injection_depth": b.injection_depth, "injection_order": b.injection_order}
    return loc.value


@PROMPT.action("set", "path + value：把該處設成 value（整段改寫內容、開關區塊、改參數）", required=("path",))
def prompt_set(ctx: Ctx, a: PromptArgs) -> dict:
    doc = _preset(ctx).model_dump(mode="json")
    loc = dp.locate(doc, a.path, PRESET_SPEC)
    _check_marker(loc)
    if loc.names == ("blocks",):
        raise ToolError("不能整個取代區塊；請改區塊底下的欄位（例如 blocks[<id>].content）")
    old = loc.value
    dp.set_value(doc, a.path, a.value, PRESET_SPEC, _preset_gate(ctx))
    _save_preset(ctx, doc)
    if isinstance(old, str) and isinstance(a.value, str):
        return {"ok": True, "path": a.path, "chars": f"{len(old)} → {len(a.value)}"}
    return {"ok": True, "path": a.path, "old": old, "new": a.value}


@PROMPT.action("patch", "path + find + replace：在文字欄位中把唯一出現的一段原文替換掉（小修改優先用這個，省 token）",
               required=("path", "find", "replace"))
def prompt_patch(ctx: Ctx, a: PromptArgs) -> dict:
    doc = _preset(ctx).model_dump(mode="json")
    loc = dp.locate(doc, a.path, PRESET_SPEC)
    _check_marker(loc)
    new = _patch(loc.value, a.find, a.replace, a.path)
    dp.set_value(doc, a.path, new, PRESET_SPEC, _preset_gate(ctx))
    _save_preset(ctx, doc)
    return {"ok": True, "path": a.path, "chars": len(new)}


@PROMPT.action("add", "value（{name, content, role?, enabled?, injection_position?, injection_depth?}）+ after?：新增內容區塊",
               required=("value",))
def prompt_add(ctx: Ctx, a: PromptArgs) -> dict:
    if not isinstance(a.value, dict):
        raise ToolError("value 必須是區塊物件 {name, content, role?, enabled?}")
    doc = _preset(ctx).model_dump(mode="json")
    pos = _locate_block(doc, f"blocks[{a.after}]").key + 1 if a.after else 0
    value = {k: v for k, v in a.value.items() if k not in ("id", "kind", "marker", "st")}
    block = Block.model_validate(value)
    doc["blocks"].insert(pos, block.model_dump(mode="json"))
    _save_preset(ctx, doc)
    return {"ok": True, "id": block.id, "index": pos}


@PROMPT.action("remove", "path（blocks[<id>]）：刪除區塊；marker 不能刪，只想暫時停用請 set …enabled = false", required=("path",))
def prompt_remove(ctx: Ctx, a: PromptArgs) -> dict:
    doc = _preset(ctx).model_dump(mode="json")
    loc = _locate_block(doc, a.path)
    if loc.value.get("kind") == "marker":
        raise ToolError("marker 區塊不能刪除")
    removed = doc["blocks"].pop(loc.key)
    _save_preset(ctx, doc)
    return {"ok": True, "removed": removed["name"]}


@PROMPT.action("move", "path（blocks[<id>]）+ after?：移動區塊，放到 after 區塊之後（省略時移到最前面）", required=("path",))
def prompt_move(ctx: Ctx, a: PromptArgs) -> dict:
    doc = _preset(ctx).model_dump(mode="json")
    loc = _locate_block(doc, a.path)
    block = loc.value
    if a.after and _locate_block(doc, f"blocks[{a.after}]").value["id"] == block["id"]:
        raise ToolError("after 不能是自己")
    doc["blocks"].pop(loc.key)
    pos = _locate_block(doc, f"blocks[{a.after}]").key + 1 if a.after else 0
    doc["blocks"].insert(pos, block)
    _save_preset(ctx, doc)
    return {"ok": True, "name": block["name"], "index": pos}


@PROMPT.action("preview", "scenario_id?：組裝工作區，回傳實際送給模型的 messages（含每段來自哪些區塊）與組裝警告")
def prompt_preview(ctx: Ctx, a: PromptArgs) -> dict:
    ws = get_workspace(ctx.conn)
    context = ws.context.model_copy(update={"scenario_id": a.scenario_id}) if a.scenario_id else ws.context
    scenario = resolve_scenario(ctx.conn, context)
    res = build_for(ws.preset, scenario, context)
    return {
        "scenario": scenario.name,
        "total_tokens": res.total_tokens,
        "warnings": res.warnings,
        "messages": [
            {
                "role": m.role,
                "sources": sorted({s.name for s in m.sources if s.name}),
                "content": _clip(m.content, a.max_chars),
            }
            for m in res.messages
        ],
    }


def _preset_diff(old: Preset, new: Preset) -> dict:
    d = vcs.diff_presets(old, new)
    return {
        "summary": d["summary"],
        "params": d["params"],
        "blocks": [{"name": b["name"], "status": b["status"], "fields": sorted(b["changes"])}
                   for b in d["blocks"] if "unchanged" not in b["status"]],
    }


@PROMPT.action("diff", "列出工作區相對於基準版本改了什麼（提交前檢查用）")
def prompt_diff(ctx: Ctx, _: PromptArgs) -> dict:
    _, base = workspace.head(ctx.conn)
    old = vcs.read_tree(ctx.conn, vcs.get_commit(ctx.conn, base).tree_id) if base else Preset(name="")
    return _preset_diff(old, _preset(ctx))


@PROMPT.action("commit", "message：提交工作區，結束這一輪修改；在受保護的分支上時會自動建立新分支", required=("message",))
def prompt_commit(ctx: Ctx, a: PromptArgs) -> dict:
    branch = workspace.ensure_unprotected_branch(ctx.conn, ctx.branch_prefix)
    c = workspace.commit(ctx.conn, a.message, author=ctx.author, meta=ctx.meta)
    ctx.committed = {"commit": c.id, "branch": branch}
    return {"ok": True, "commit": c.id[:8], "branch": branch}


# =====================================================================
# scenario：情境（角色卡 + 測試條件）
# =====================================================================

class ScenarioArgs(BaseModel):
    action: Literal["list", "read", "set", "patch", "add", "remove", "diff", "revisions", "commit"]
    scenario_id: int | None = Field(None, description="情境 id（內建迴圈改角色卡時可省略）")
    path: str | None = Field(None, description="欄位路徑，例如 card.description、lore[3].content、user_turns[0]")
    value: Any = Field(None, description="set：新的值（任何 JSON 型別）；add：要加入清單的項目")
    find: str | None = Field(None, description="patch：要替換的原文，必須恰好出現一次")
    replace: str | None = Field(None, description="patch：替換成的文字；空字串代表刪除")
    index: int | None = Field(None, description="add：插入位置；省略時加在最後")
    message: str | None = Field(None, description="commit：版本說明（改了哪些欄位 / 條目、針對哪些評語、預期效果）")


SCENARIO = Tool(
    "scenario",
    "情境頁：讀取與修改情境的角色卡（所有欄位與世界書）和測試條件，存成新版本。每次提交都是一個版本，可在情境頁 diff 與還原。",
    ScenarioArgs,
    guide=(
        "路徑——角色卡：card.name / description / personality / scenario / first_mes / mes_example / creator_notes / "
        "system_prompt / post_history_instructions / creator / character_version、card.tags、card.alternate_greetings[i]、"
        "card.depth_prompt.prompt / depth / role、card.regex_scripts[i].<欄位>、card.book.name / scan_depth / token_budget / recursive_scanning；"
        "世界書條目 lore[<uid>] 之下有 content、comment、keys、secondary_keys、enabled、constant、selective、selective_logic（0–3）、"
        "position（0 角色前、1 角色後、2 作者注上、3 作者注下、4 @深度、5 範例前、6 範例後、7 outlet）、depth、role（0 system、1 user、2 assistant）、"
        "order、probability（0–100）、group 等；新增條目用 add path=lore、value={comment, keys, content, …}（uid 自動配）。"
        "開場白（這個情境實際使用的第一則訊息）：opening。"
        "測試條件：persona、user_goal、user_turns[i]、history[i].content / role、vars.<名稱>；其他：name、notes、char、user。"
    ),
)

SCENARIO_SPEC = dp.Spec(
    keyed={("card", "book", "entries"): "uid"},
    aliases={"lore": "card.book.entries"},
    free={("vars",), ("card", "regex_scripts")},
    blocked={("id",), ("updated_at",), ("card", "raw"), ("card", "spec"), ("card", "book", "entries", "uid")},
)
LORE_RANGES = {"position": (0, 7), "probability": (0, 100), "role": (0, 2), "selective_logic": (0, 3), "depth": (0, None)}


def _scenario(ctx: Ctx, sid: int | None) -> Scenario:
    sid = sid if sid is not None else ctx.scenario_id
    if sid is None:
        raise ToolError("需要 scenario_id（用 action=list 查）")
    if ctx.scenario_id is not None and sid != ctx.scenario_id:
        raise ToolError(f"這一輪只能修改情境 #{ctx.scenario_id}")
    sc = store.get_scenario(ctx.conn, sid)
    if sc is None:
        raise ToolError(f"找不到情境 #{sid}（用 action=list 查 id）")
    return sc


def _scenario_doc(sc: Scenario, path: str | None) -> dict:
    doc = sc.model_dump(mode="json")
    if path and doc["card"] is None and (path.startswith("card") or path.startswith("lore")):
        raise ToolError(f"情境 #{sc.id} 沒有角色卡（手動建立的情境只有測試條件）")
    return doc


def _scenario_gate(ctx: Ctx):
    def check(names: tuple[str, ...]) -> str | None:
        if names[0] in revisions.FIXTURE_FIELDS and not ctx.settings.allow_fixtures:
            return "測試條件不開放修改（設定頁 → 修改者 LLM → 允許修改測試條件）"
        return None

    return check


def _save_scenario(ctx: Ctx, sid: int, doc: dict) -> Scenario:
    sc = Scenario.model_validate(doc)
    sc.id = sid
    book = sc.card.book if sc.card else None
    if book:
        uids = [e.uid for e in book.entries]
        if len(uids) != len(set(uids)):
            raise ToolError("世界書條目的 uid 重複")
        for e in book.entries:
            for k, (lo, hi) in LORE_RANGES.items():
                v = getattr(e, k)
                if v < lo or (hi is not None and v > hi):
                    raise ToolError(f"lore[{e.uid}].{k} 必須在 {lo}～{hi if hi is not None else '∞'} 之間")
    if sc.card and sc.card.depth_prompt.depth < 0:
        raise ToolError("card.depth_prompt.depth 不能是負數")
    store.save_scenario(ctx.conn, sc)
    ctx.changed = True
    return sc


@SCENARIO.action("list", "列出情境（id、名稱、角色、是否有角色卡）")
def scenario_list(ctx: Ctx, _: ScenarioArgs) -> list[dict]:
    return [{"id": s.id, "name": s.name, "char": s.char, "user": s.user, "has_card": s.card is not None}
            for s in store.list_scenarios(ctx.conn)]


@SCENARIO.action("read", "scenario_id（+ path?）：不帶 path 回傳角色卡全文、世界書條目清單、測試條件與目前版本；帶 path 讀該處的值")
def scenario_read(ctx: Ctx, a: ScenarioArgs) -> Any:
    sc = _scenario(ctx, a.scenario_id)
    if a.path:
        return dp.locate(_scenario_doc(sc, a.path), a.path, SCENARIO_SPEC).value
    revs = revisions.list_revisions(ctx.conn, sc.id)
    out: dict = {
        "scenario_id": sc.id, "name": sc.name, "char": sc.char, "user": sc.user,
        "version": revs[0]["number"] if revs else None,
        "unsaved_changes": revisions.is_dirty(ctx.conn, sc),
        "opening": sc.opening,
        "fixtures": {
            "editable": ctx.settings.allow_fixtures,
            "persona": sc.persona, "user_goal": sc.user_goal, "user_turns": sc.user_turns, "vars": sc.vars,
            "history": [{"role": m.role, "content": _clip(m.content, 400)} for m in sc.history],
        },
    }
    if sc.card is None:
        out["card"] = None
        return out
    card = sc.card.model_dump(mode="json", exclude={"raw", "spec", "book"})
    card["regex_scripts"] = [{"index": i, "scriptName": r.get("scriptName", ""), "disabled": r.get("disabled", False)}
                             for i, r in enumerate(sc.card.regex_scripts)]
    out["card"] = card
    book = sc.card.book
    out["book"] = None if book is None else {
        **book.model_dump(exclude={"entries"}),
        "entries": [{"uid": e.uid, "comment": e.comment, "keys": e.keys, "enabled": e.enabled, "constant": e.constant,
                     "position": e.position, "chars": len(e.content)} for e in book.entries],
    }
    return out


@SCENARIO.action("set", "scenario_id + path + value：把該處設成 value", required=("path",))
def scenario_set(ctx: Ctx, a: ScenarioArgs) -> dict:
    sc = _scenario(ctx, a.scenario_id)
    doc = _scenario_doc(sc, a.path)
    old = dp.locate(doc, a.path, SCENARIO_SPEC).value
    dp.set_value(doc, a.path, a.value, SCENARIO_SPEC, _scenario_gate(ctx))
    _save_scenario(ctx, sc.id, doc)
    if isinstance(old, str) and isinstance(a.value, str):
        return {"ok": True, "path": a.path, "chars": f"{len(old)} → {len(a.value)}"}
    return {"ok": True, "path": a.path, "old": old, "new": a.value}


@SCENARIO.action("patch", "scenario_id + path + find + replace：在文字欄位中把唯一出現的一段原文替換掉",
                 required=("path", "find", "replace"))
def scenario_patch(ctx: Ctx, a: ScenarioArgs) -> dict:
    sc = _scenario(ctx, a.scenario_id)
    doc = _scenario_doc(sc, a.path)
    loc = dp.locate(doc, a.path, SCENARIO_SPEC)
    new = _patch(loc.value, a.find, a.replace, a.path)
    dp.set_value(doc, a.path, new, SCENARIO_SPEC, _scenario_gate(ctx))
    _save_scenario(ctx, sc.id, doc)
    return {"ok": True, "path": a.path, "chars": len(new)}


@SCENARIO.action("add", "scenario_id + path（清單）+ value + index?：在清單中加入一項（例如 lore、card.alternate_greetings、user_turns）",
                 required=("path",))
def scenario_add(ctx: Ctx, a: ScenarioArgs) -> dict:
    sc = _scenario(ctx, a.scenario_id)
    doc = _scenario_doc(sc, a.path)
    tokens = dp.parse(a.path, SCENARIO_SPEC)
    names = tuple(k for kind, k in tokens if kind == "field")
    value = a.value
    if names[:2] == ("card", "book") and doc["card"]["book"] is None:
        doc["card"]["book"] = Lorebook(name=sc.card.name or sc.char).model_dump(mode="json")
    if names == ("card", "book", "entries"):
        if not isinstance(value, dict):
            raise ToolError("世界書條目必須是物件，例如 {comment, keys, content}")
        entries = doc["card"]["book"]["entries"]
        value = {**value, "uid": max((e["uid"] for e in entries), default=-1) + 1}
    _loc, pos = dp.insert(doc, a.path, value, SCENARIO_SPEC, a.index, _scenario_gate(ctx))
    _save_scenario(ctx, sc.id, doc)
    out = {"ok": True, "path": a.path, "index": pos}
    if names == ("card", "book", "entries"):
        out["uid"] = value["uid"]
    return out


@SCENARIO.action("remove", "scenario_id + path（清單元素，例如 lore[3]、card.alternate_greetings[1]、vars.<名稱>）：刪除該項",
                 required=("path",))
def scenario_remove(ctx: Ctx, a: ScenarioArgs) -> dict:
    sc = _scenario(ctx, a.scenario_id)
    doc = _scenario_doc(sc, a.path)
    removed = dp.remove(doc, a.path, SCENARIO_SPEC, _scenario_gate(ctx))
    _save_scenario(ctx, sc.id, doc)
    label = removed.get("comment") if isinstance(removed, dict) else removed
    return {"ok": True, "removed": _clip(str(label), 200)}


def _scenario_changes(ctx: Ctx, sc: Scenario) -> dict | None:
    last = revisions.latest(ctx.conn, sc.id)
    if last is None:
        return None
    return revisions.diff(revisions.scenario_of(revisions.get(ctx.conn, last["id"]), sc.id), sc)


@SCENARIO.action("diff", "scenario_id：列出相對於最新版本改了什麼（提交前檢查用）")
def scenario_diff(ctx: Ctx, a: ScenarioArgs) -> dict:
    sc = _scenario(ctx, a.scenario_id)
    d = _scenario_changes(ctx, sc)
    if d is None:
        return {"note": "還沒有任何版本，提交後會成為第一版"}
    return {
        "fields": [{"path": f["path"], "label": f["label"], "group": f["group"]} for f in d["fields"]],
        "lore": [{"uid": x["uid"], "comment": x["comment"], "status": x["status"], "changes": x["changes"]} for x in d["lore"]],
        "fixture_changed": d["fixture_changed"],
    }


@SCENARIO.action("revisions", "scenario_id：版本紀錄（新的在前）")
def scenario_revisions(ctx: Ctx, a: ScenarioArgs) -> list[dict]:
    sc = _scenario(ctx, a.scenario_id)
    return [{"number": r["number"], "id": r["id"], "author": r["author"], "message": r["message"], "created_at": r["created_at"]}
            for r in revisions.list_revisions(ctx.conn, sc.id)[:30]]


@SCENARIO.action("commit", "scenario_id + message：把目前內容存成新版本，結束這一輪修改", required=("message",))
def scenario_commit(ctx: Ctx, a: ScenarioArgs) -> dict:
    sc = _scenario(ctx, a.scenario_id)
    d = _scenario_changes(ctx, sc)
    fixture = bool(d and d["fixture_changed"])
    message = ("【測試條件變更】" if fixture else "") + a.message
    rev, created = revisions.create(ctx.conn, sc, author=ctx.author, message=message,
                                    meta={**ctx.meta, "fixture_changed": fixture} if fixture else ctx.meta)
    if not created:
        raise ToolError("沒有變更可以提交")
    ctx.committed = {"revision": rev["id"], "scenario_id": sc.id}
    out = {"ok": True, "revision": rev["id"]}
    if fixture:
        out["note"] = "這一版改了測試條件；之後的實驗結果不能和之前直接比較"
    return out


# =====================================================================
# experiment：實驗、評語、回覆
# =====================================================================

class ExperimentArgs(BaseModel):
    action: Literal["list", "read", "feedback", "reply", "run"]
    experiment_id: int | None = Field(None, description="read / feedback：實驗 id；run：重跑這個實驗（同一腳本快照與情境）")
    run_id: int | None = Field(None, description="feedback：只看這次試跑的評語")
    message_id: int | None = Field(None, description="reply：訊息 id")
    reply_chars: int = Field(2500, description="feedback：附上的回覆原文最多幾字")
    script_id: int | None = Field(None, description="run（新實驗）：腳本 id")
    scenario_ids: list[int] | None = Field(None, description="run（新實驗）：情境 id")
    connection_id: int | None = Field(None, description="run（新實驗）：連線 id")
    name: str = Field("", description="run（新實驗）：實驗名稱")


EXPERIMENT = Tool(
    "experiment",
    "實驗頁：讀取實驗結果、使用者的評語（含分數與自動指標）與被評的回覆；外部 agent 可在開啟後執行實驗。",
    ExperimentArgs,
)


@EXPERIMENT.action("list", "列出實驗（狀態、階段、平均分、花費）", builtin=False)
def experiment_list(ctx: Ctx, _: ExperimentArgs) -> list[dict]:
    return experiments.list_experiments(ctx.conn)[:50]


@EXPERIMENT.action("read", "experiment_id：各階段分支的狀態、分數、摘錄、是否晉級", required=("experiment_id",), builtin=False)
def experiment_read(ctx: Ctx, a: ExperimentArgs) -> dict:
    d = experiments.detail(ctx.conn, a.experiment_id)
    if d is None:
        raise ToolError(f"找不到實驗 #{a.experiment_id}")
    keep = ("id", "stage", "scenario_id", "seed", "temperature", "parent_run_id", "status", "score", "promoted", "excerpt", "last_turn")
    return {
        "id": d["id"], "name": d["name"], "status": d["status"], "current_stage": d["current_stage"],
        "preset": d["preset_name"], "commit": (d["commit_id"] or "")[:8], "script": d["script_name"],
        "stages": d["script_snapshot"]["stages"], "cost": d["cost"],
        "runs": [{k: r[k] for k in keep} for r in d["runs"]],
    }


@EXPERIMENT.action("feedback", "experiment_id? / run_id?：使用者的評語（含分數）與被評的回覆原文、自動指標；都不帶時回傳最近 30 則")
def experiment_feedback(ctx: Ctx, a: ExperimentArgs) -> dict:
    conn = ctx.conn
    if a.experiment_id is not None:
        run_ids = [r["id"] for r in conn.execute("SELECT id FROM runs WHERE experiment_id = ?", (a.experiment_id,))]
        items = [f for rid in run_ids for f in feedback.list_for(conn, run_id=rid)]
    elif a.run_id is not None:
        items = feedback.list_for(conn, run_id=a.run_id)
    else:
        items = feedback.list_for(conn)[-30:]
    defs = metrics.load(conn)
    names = {m.id: m.name for m in defs}

    def measured(text: str) -> dict:
        return {names[k]: v for k, v in metrics.measure(text, defs).items()}

    out = []
    for f in items:
        entry: dict = {"feedback_id": f["id"], "scores": f["scores"], "text": f["text"]}
        if f["target_type"] == "run_message":
            m = conn.execute("SELECT id, run_id, turn, role, content FROM run_messages WHERE id = ?", (f["target_id"],)).fetchone()
            if m:
                entry |= {"target": f"回覆 message_id={m['id']}（試跑 #{m['run_id']} 第 {m['turn']} 回合）",
                          "reply": _clip(m["content"], a.reply_chars), "reply_metrics": measured(m["content"])}
        elif f["target_type"] == "run":
            m = conn.execute(
                "SELECT id, turn, content FROM run_messages WHERE run_id = ? AND role = 'assistant' ORDER BY idx DESC LIMIT 1",
                (f["target_id"],),
            ).fetchone()
            entry["target"] = f"試跑 #{f['target_id']} 整體"
            if m:
                entry["last_reply"] = _clip(m["content"], a.reply_chars)
                entry["last_reply_message_id"] = m["id"]
                entry["last_reply_metrics"] = measured(m["content"])
        else:
            entry["target"] = f"單次生成 #{f['target_id']}"
        out.append(entry)
    return {"count": len(out), "feedback": out}


@EXPERIMENT.action("reply", "message_id：一則試跑訊息的完整原文，以及它之前的使用者發言", required=("message_id",))
def experiment_reply(ctx: Ctx, a: ExperimentArgs) -> dict:
    m = ctx.conn.execute("SELECT * FROM run_messages WHERE id = ?", (a.message_id,)).fetchone()
    if m is None:
        raise ToolError(f"找不到訊息 #{a.message_id}")
    prev = ctx.conn.execute(
        "SELECT content FROM run_messages WHERE run_id = ? AND idx < ? AND role = 'user' ORDER BY idx DESC LIMIT 1",
        (m["run_id"], m["idx"]),
    ).fetchone()
    return {
        "run_id": m["run_id"], "turn": m["turn"], "role": m["role"],
        "previous_user_message": prev["content"] if prev else None,
        "content": _clip(m["content"], 20000), "finish_reason": m["finish_reason"],
    }


@EXPERIMENT.action("run", "experiment_id（重跑該實驗）或 script_id + scenario_ids + connection_id（新實驗）：用目前工作區的預設開始實驗，"
                   "只跑第一階段，晉級由使用者在網頁上確認（會產生 LLM 費用）", builtin=False, gate="allow_run_experiment")
def experiment_run(ctx: Ctx, a: ExperimentArgs) -> dict:
    if ctx.manager is None:
        raise ToolError("這個接口無法執行實驗")
    preset = _preset(ctx)
    if a.experiment_id is not None:
        exp_id = experiments.create_rerun(ctx.conn, ctx.manager, a.experiment_id, preset)
    else:
        if a.script_id is None or a.connection_id is None or not a.scenario_ids:
            raise ToolError("需要 script_id、scenario_ids 與 connection_id（或用 experiment_id 重跑）")
        exp_id = experiments.create_and_start(ctx.conn, ctx.manager, name=a.name, preset=preset, script_id=a.script_id,
                                              scenario_ids=a.scenario_ids, connection_id=a.connection_id)
    return {"ok": True, "experiment_id": exp_id, "note": "實驗在背景執行；用 action=read 查看進度"}


# =====================================================================
# script：金字塔腳本（唯讀）
# =====================================================================

class ScriptArgs(BaseModel):
    action: Literal["list", "read"]
    script_id: int | None = Field(None, description="read：腳本 id")


SCRIPT = Tool("script", "腳本頁：讀取金字塔腳本（解析後的階段設定）。", ScriptArgs)


@SCRIPT.action("list", "列出腳本（id、名稱、階段數）", builtin=False)
def script_list(ctx: Ctx, _: ScriptArgs) -> list[dict]:
    from . import scripts

    out = []
    for s in scripts.list_all(ctx.conn):
        try:
            data, _w = scripts.resolve(ctx.conn, s.id)
            out.append({"id": s.id, "name": s.name, "stages": len(data.stages)})
        except ValueError as e:
            out.append({"id": s.id, "name": s.name, "error": str(e)})
    return out


@SCRIPT.action("read", "script_id：腳本解析後的完整階段設定", required=("script_id",), builtin=False)
def script_read(ctx: Ctx, a: ScriptArgs) -> dict:
    from . import scripts

    s = scripts.get(ctx.conn, a.script_id)
    if s is None:
        raise ToolError(f"找不到腳本 #{a.script_id}")
    data, warnings = scripts.resolve(ctx.conn, s.id)
    return {"id": s.id, "name": s.name, "warnings": warnings, **data.model_dump()}


# =====================================================================
# versions：預設的版本樹（唯讀）
# =====================================================================

class VersionsArgs(BaseModel):
    action: Literal["log", "diff"]
    commit: str | None = Field(None, description="diff：commit id（或前綴）")
    against: str | None = Field(None, description="diff：和哪個 commit 比；省略時和它的上一版比")
    limit: int = Field(20, description="log：最多幾筆")


VERSIONS = Tool("versions", "版本頁：預設的提交紀錄、分支與兩個版本的差異。", VersionsArgs)


@VERSIONS.action("log", "最近的提交（作者、說明、平均分）與分支", builtin=False)
def versions_log(ctx: Ctx, a: VersionsArgs) -> dict:
    scores = feedback.overall_by_commit(ctx.conn)
    branch, base = workspace.head(ctx.conn)
    return {
        "head": {"branch": branch, "base_commit": base[:8] if base else None},
        "branches": [{"name": r["name"], "commit": r["commit_id"][:8], "protected": r["protected"]}
                     for r in vcs.list_refs(ctx.conn)],
        "commits": [{"id": c.id[:8], "parents": [p[:8] for p in c.parents], "author": c.author, "message": c.message,
                     "created_at": c.created_at, "score": scores.get(c.id)}
                    for c in vcs.log(ctx.conn, max(1, min(a.limit, 200)))],
    }


@VERSIONS.action("diff", "commit + against?：兩個版本之間改了哪些區塊與參數", required=("commit",), builtin=False)
def versions_diff(ctx: Ctx, a: VersionsArgs) -> dict:
    new = vcs.get_commit(ctx.conn, a.commit)
    if a.against:
        old_tree = vcs.get_commit(ctx.conn, a.against).tree_id
    elif new.parents:
        old_tree = vcs.get_commit(ctx.conn, new.parents[0]).tree_id
    else:
        old_tree = None
    old = vcs.read_tree(ctx.conn, old_tree) if old_tree else Preset(name="")
    return _preset_diff(old, vcs.read_tree(ctx.conn, new.tree_id))


REGISTRY: dict[str, Tool] = {t.name: t for t in (PROMPT, SCENARIO, EXPERIMENT, SCRIPT, VERSIONS)}
