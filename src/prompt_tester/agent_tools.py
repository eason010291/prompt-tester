"""修改者 LLM 的工具（design.md §7.3）：單一來源的定義，供 ① 內建迴圈 ② MCP ③ REST 共用。

- 每個工具 = 名稱 + 說明 + pydantic 參數模型（轉成 JSON Schema）+ 實作。
- 只能操作工作區；提交時若目前在受保護的分支（main）上，自動建立新分支，LLM 不會推進受保護分支。
- 花錢的工具（run_experiment）只給外部 agent，且需要在設定中開啟。
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from . import experiments, feedback, store, vcs, workspace
from .preset import POSITION_IN_CHAT, Block, Preset
from .routes.prompt import build_for, get_workspace, resolve_scenario

AGENT_KEY = "agent"
MAX_RESULT_CHARS = 16000

DEFAULT_SYSTEM_PROMPT = """你是 SillyTavern 角色扮演提示詞（Chat Completion 預設）的修改者。使用者用這個工具測試預設，對生成結果寫了評語；你的工作是依評語修改預設，讓下一輪生成更好。

工作方式：
1. 先讀評語與被評的回覆，找出問題的根本原因屬於哪個區塊。必要時用 get_block 讀全文、用 preview 看組裝後實際送出的內容。
2. 做「最小、有針對性」的修改：優先用 patch_block 局部替換；只有需要整段重寫時才用 edit_block。不要順手改寫沒被提到的地方。
3. 保留預設的既有結構與慣例：巨集（{{char}}、{{user}}、{{getvar::…}} 等）、XML / HTML 標籤與狀態欄格式（使用者的正則依賴這些標籤）、區塊的語言與語氣。marker 區塊（聊天歷史、角色描述等）沒有內容，不要動。
4. 評語彼此矛盾或資訊不足時，選擇保守的改法，並在提交說明中寫出取捨。
5. 改完後呼叫 commit 一次，說明要條列：改了哪些區塊、各自針對哪條評語、預期效果。提交後就結束，不需要再回覆。

注意：分數是使用者自訂的「屬性：n/10」，低分的屬性通常是重點。"""


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


def load_settings(conn: sqlite3.Connection) -> AgentSettings:
    return AgentSettings.model_validate(store.get_setting(conn, AGENT_KEY) or {})


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


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[Ctx, Any], Any]
    builtin: bool = True  # 內建迴圈預設提供
    gate: str | None = None  # 需要開啟的設定欄位

    def schema(self) -> dict:
        s = self.args.model_json_schema()
        s.pop("title", None)
        for p in s.get("properties", {}).values():
            p.pop("title", None)
        return s


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, args: type[BaseModel], *, builtin: bool = True, gate: str | None = None):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, args, fn, builtin, gate)
        return fn

    return deco


def call(ctx: Ctx, name: str, raw_args: dict | str | None) -> Any:
    """執行工具；錯誤以 ToolError 回報（訊息給 LLM 看，讓它修正後重試）。"""
    t = REGISTRY.get(name)
    if t is None:
        raise ToolError(f"沒有這個工具：{name}")
    if t.gate and not getattr(ctx.settings, t.gate):
        raise ToolError(f"工具 {name} 未開啟（設定頁 → 修改者 LLM）")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            raise ToolError(f"參數不是合法的 JSON：{e.msg}") from e
    try:
        args = t.args.model_validate(raw_args or {})
    except ValidationError as e:
        raise ToolError("參數錯誤：" + "；".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors())) from e
    try:
        return t.fn(ctx, args)
    except (vcs.VcsError, experiments.ExperimentError, KeyError, ValueError) as e:
        raise ToolError(str(e).strip("'")) from e


def openai_tools(names: list[str] | None = None) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema()}}
        for t in REGISTRY.values()
        if names is None or t.name in names
    ]


def builtin_names(settings: AgentSettings) -> list[str]:
    return [t.name for t in REGISTRY.values() if t.builtin and (not t.gate or getattr(settings, t.gate))]


def to_text(result: Any, limit: int = MAX_RESULT_CHARS) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"…（已截斷，共 {len(text)} 字）"


# ---- 工作區 ----

def _preset(ctx: Ctx) -> Preset:
    return workspace.working(ctx.conn)


def _save(ctx: Ctx, preset: Preset) -> None:
    workspace.save_preset(ctx.conn, preset)
    ctx.changed = True


def _find(preset: Preset, block_id: str) -> tuple[int, Block]:
    """完整 id 或至少 6 碼的唯一前綴。"""
    hits = [(i, b) for i, b in enumerate(preset.blocks) if b.id == block_id]
    if not hits and len(block_id) >= 6:
        hits = [(i, b) for i, b in enumerate(preset.blocks) if b.id.startswith(block_id)]
    if not hits:
        raise ToolError(f"找不到區塊 {block_id}（用 list_blocks 查 id）")
    if len(hits) > 1:
        raise ToolError(f"區塊 id 前綴 {block_id} 不唯一")
    return hits[0]


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


class NoArgs(BaseModel):
    pass


@tool("list_blocks", "列出工作區預設的所有區塊（順序即組裝順序）、分支與是否有未提交的變更。", NoArgs)
def list_blocks(ctx: Ctx, _: NoArgs) -> dict:
    p = _preset(ctx)
    branch, base = workspace.head(ctx.conn)
    return {
        "preset": p.name,
        "branch": branch,
        "base_commit": base[:8] if base else None,
        "uncommitted_changes": workspace.is_dirty(ctx.conn),
        "params": p.params.model_dump(),
        "blocks": [_summary(b, i) for i, b in enumerate(p.blocks)],
    }


class BlockId(BaseModel):
    id: str = Field(description="區塊 id（或至少 6 碼的唯一前綴）")


@tool("get_block", "讀取一個區塊的完整內容與設定。", BlockId)
def get_block(ctx: Ctx, a: BlockId) -> dict:
    i, b = _find(_preset(ctx), a.id)
    return _summary(b, i) | {"content": b.content, "injection_depth": b.injection_depth, "injection_order": b.injection_order}


class EditBlock(BaseModel):
    id: str = Field(description="區塊 id")
    content: str = Field(description="新的完整內容")


@tool("edit_block", "以新內容取代整個區塊（大幅改寫時用；小修改請用 patch_block）。", EditBlock)
def edit_block(ctx: Ctx, a: EditBlock) -> dict:
    p = _preset(ctx)
    _, b = _find(p, a.id)
    if b.kind == "marker":
        raise ToolError("marker 區塊的內容由情境填入，不能編輯")
    old = len(b.content)
    b.content = a.content
    _save(ctx, p)
    return {"ok": True, "name": b.name, "chars": f"{old} → {len(a.content)}"}


class PatchBlock(BaseModel):
    id: str = Field(description="區塊 id")
    find: str = Field(description="要替換的原文片段，必須在區塊中恰好出現一次（含空白與換行）")
    replace: str = Field(description="替換成的文字；空字串代表刪除該片段")


@tool("patch_block", "在區塊中把唯一出現的一段原文替換掉（省 token 的局部修改）。find 找不到或出現多次時會報錯。", PatchBlock)
def patch_block(ctx: Ctx, a: PatchBlock) -> dict:
    p = _preset(ctx)
    _, b = _find(p, a.id)
    if not a.find:
        raise ToolError("find 不能是空的")
    n = b.content.count(a.find)
    if n == 0:
        raise ToolError("find 在區塊中找不到；請用 get_block 讀原文後逐字複製")
    if n > 1:
        raise ToolError(f"find 在區塊中出現 {n} 次，請加長片段使其唯一")
    b.content = b.content.replace(a.find, a.replace, 1)
    _save(ctx, p)
    return {"ok": True, "name": b.name, "chars": len(b.content)}


class AddBlock(BaseModel):
    name: str
    content: str
    role: Literal["system", "user", "assistant"] = "system"
    after_id: str | None = Field(None, description="放在這個區塊之後；省略時放在最前面")
    enabled: bool = True


@tool("add_block", "新增一個內容區塊。", AddBlock)
def add_block(ctx: Ctx, a: AddBlock) -> dict:
    p = _preset(ctx)
    b = Block(name=a.name, content=a.content, role=a.role, enabled=a.enabled)
    pos = _find(p, a.after_id)[0] + 1 if a.after_id else 0
    p.blocks.insert(pos, b)
    _save(ctx, p)
    return {"ok": True, "id": b.id, "index": pos}


@tool("remove_block", "刪除一個內容區塊（marker 不能刪；只想暫時停用請用 toggle_block）。", BlockId)
def remove_block(ctx: Ctx, a: BlockId) -> dict:
    p = _preset(ctx)
    i, b = _find(p, a.id)
    if b.kind == "marker":
        raise ToolError("marker 區塊不能刪除")
    p.blocks.pop(i)
    _save(ctx, p)
    return {"ok": True, "removed": b.name}


class MoveBlock(BaseModel):
    id: str
    after_id: str | None = Field(None, description="移到這個區塊之後；省略時移到最前面")


@tool("move_block", "移動區塊在組裝順序中的位置。", MoveBlock)
def move_block(ctx: Ctx, a: MoveBlock) -> dict:
    p = _preset(ctx)
    i, b = _find(p, a.id)
    if a.after_id and _find(p, a.after_id)[1].id == b.id:
        raise ToolError("after_id 不能是自己")
    p.blocks.pop(i)
    pos = _find(p, a.after_id)[0] + 1 if a.after_id else 0
    p.blocks.insert(pos, b)
    _save(ctx, p)
    return {"ok": True, "name": b.name, "index": pos}


class ToggleBlock(BaseModel):
    id: str
    enabled: bool


@tool("toggle_block", "啟用或停用一個區塊。", ToggleBlock)
def toggle_block(ctx: Ctx, a: ToggleBlock) -> dict:
    p = _preset(ctx)
    _, b = _find(p, a.id)
    b.enabled = a.enabled
    _save(ctx, p)
    return {"ok": True, "name": b.name, "enabled": b.enabled}


class SetParams(BaseModel):
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    frequency_penalty: float | None = Field(None, ge=-2, le=2)
    presence_penalty: float | None = Field(None, ge=-2, le=2)
    max_tokens: int | None = Field(None, ge=1)


@tool("set_params", "修改取樣參數（只傳要改的欄位）。", SetParams, gate="allow_params")
def set_params(ctx: Ctx, a: SetParams) -> dict:
    p = _preset(ctx)
    changes = a.model_dump(exclude_none=True)
    if not changes:
        raise ToolError("沒有要修改的欄位")
    p.params = p.params.model_copy(update=changes)
    _save(ctx, p)
    return {"ok": True, "params": p.params.model_dump()}


class Preview(BaseModel):
    scenario_id: int | None = Field(None, description="情境 id；省略時用提示詞頁目前選的情境")
    max_chars: int = Field(1500, description="每則訊息最多回傳幾字")


@tool("preview", "組裝工作區預設，回傳實際送給模型的 messages（含每段來自哪些區塊）與組裝警告。", Preview)
def preview(ctx: Ctx, a: Preview) -> dict:
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
                "content": m.content if len(m.content) <= a.max_chars else m.content[: a.max_chars] + "…",
            }
            for m in res.messages
        ],
    }


@tool("diff_workspace", "列出工作區相對於基準版本改了什麼（提交前檢查用）。", NoArgs)
def diff_workspace(ctx: Ctx, _: NoArgs) -> dict:
    _, base = workspace.head(ctx.conn)
    old = vcs.read_tree(ctx.conn, vcs.get_commit(ctx.conn, base).tree_id) if base else Preset(name="")
    d = vcs.diff_presets(old, _preset(ctx))
    return {
        "summary": d["summary"],
        "params": d["params"],
        "blocks": [{"name": b["name"], "status": b["status"], "fields": sorted(b["changes"])}
                   for b in d["blocks"] if "unchanged" not in b["status"]],
    }


class CommitArgs(BaseModel):
    message: str = Field(description="提交說明：改了哪些區塊、針對哪些評語、預期效果")


@tool("commit", "提交工作區，結束這一輪修改。在受保護的分支上時會自動建立新分支。", CommitArgs)
def commit(ctx: Ctx, a: CommitArgs) -> dict:
    branch = workspace.ensure_unprotected_branch(ctx.conn, ctx.branch_prefix)
    c = workspace.commit(ctx.conn, a.message, author=ctx.author, meta=ctx.meta)
    ctx.committed = {"commit": c.id, "branch": branch}
    return {"ok": True, "commit": c.id[:8], "branch": branch}


# ---- 評語與試跑 ----

def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…（共 {len(text)} 字）"


class GetFeedback(BaseModel):
    experiment_id: int | None = Field(None, description="只看這個實驗的評語")
    run_id: int | None = Field(None, description="只看這次試跑的評語")
    reply_chars: int = Field(2500, description="附上的回覆原文最多幾字")


@tool("get_feedback", "讀取使用者的評語（含分數）與被評的回覆原文。不帶參數時回傳最近 30 則。", GetFeedback)
def get_feedback(ctx: Ctx, a: GetFeedback) -> dict:
    conn = ctx.conn
    if a.experiment_id is not None:
        run_ids = [r["id"] for r in conn.execute("SELECT id FROM runs WHERE experiment_id = ?", (a.experiment_id,))]
        items = [f for rid in run_ids for f in feedback.list_for(conn, run_id=rid)]
    elif a.run_id is not None:
        items = feedback.list_for(conn, run_id=a.run_id)
    else:
        items = feedback.list_for(conn)[-30:]
    out = []
    for f in items:
        entry: dict = {"feedback_id": f["id"], "scores": f["scores"], "text": f["text"]}
        if f["target_type"] == "run_message":
            m = conn.execute("SELECT id, run_id, turn, role, content FROM run_messages WHERE id = ?", (f["target_id"],)).fetchone()
            if m:
                entry |= {"target": f"回覆 message_id={m['id']}（試跑 #{m['run_id']} 第 {m['turn']} 回合）",
                          "reply": _clip(m["content"], a.reply_chars)}
        elif f["target_type"] == "run":
            m = conn.execute(
                "SELECT id, turn, content FROM run_messages WHERE run_id = ? AND role = 'assistant' ORDER BY idx DESC LIMIT 1",
                (f["target_id"],),
            ).fetchone()
            entry["target"] = f"試跑 #{f['target_id']} 整體"
            if m:
                entry["last_reply"] = _clip(m["content"], a.reply_chars)
                entry["last_reply_message_id"] = m["id"]
        else:
            entry["target"] = f"單次生成 #{f['target_id']}"
        out.append(entry)
    return {"count": len(out), "feedback": out}


class GetReply(BaseModel):
    message_id: int


@tool("get_reply", "讀取一則試跑訊息的完整原文，以及它之前的使用者發言。", GetReply)
def get_reply(ctx: Ctx, a: GetReply) -> dict:
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


@tool("list_scenarios", "列出情境（id、名稱、角色）。", NoArgs)
def list_scenarios(ctx: Ctx, _: NoArgs) -> list[dict]:
    return [{"id": s.id, "name": s.name, "char": s.char, "user": s.user, "has_card": s.card is not None}
            for s in store.list_scenarios(ctx.conn)]


# ---- 只給外部 agent ----

@tool("list_scripts", "列出金字塔腳本（解析後的階段）。", NoArgs, builtin=False)
def list_scripts(ctx: Ctx, _: NoArgs) -> list[dict]:
    from . import scripts

    out = []
    for s in scripts.list_all(ctx.conn):
        try:
            data, _w = scripts.resolve(ctx.conn, s.id)
            out.append({"id": s.id, "name": s.name, "stages": data.model_dump()["stages"]})
        except ValueError as e:
            out.append({"id": s.id, "name": s.name, "error": str(e)})
    return out


@tool("list_experiments", "列出實驗（狀態、階段、平均分、花費）。", NoArgs, builtin=False)
def list_experiments(ctx: Ctx, _: NoArgs) -> list[dict]:
    return experiments.list_experiments(ctx.conn)[:50]


class ExperimentId(BaseModel):
    experiment_id: int


@tool("get_experiment", "讀取實驗：各階段分支的狀態、分數、摘錄、是否晉級。", ExperimentId, builtin=False)
def get_experiment(ctx: Ctx, a: ExperimentId) -> dict:
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


class RunExperiment(BaseModel):
    rerun_of: int | None = Field(None, description="用目前工作區重跑這個實驗（同一腳本快照與情境）；填了就忽略其他欄位")
    script_id: int | None = None
    scenario_ids: list[int] = Field(default_factory=list)
    connection_id: int | None = None
    name: str = ""


@tool("run_experiment", "用目前工作區的預設開始一個實驗（會產生 LLM 費用；需在設定中開啟）。只跑第一階段，晉級由使用者在網頁上確認。",
      RunExperiment, builtin=False, gate="allow_run_experiment")
def run_experiment(ctx: Ctx, a: RunExperiment) -> dict:
    if ctx.manager is None:
        raise ToolError("這個接口無法執行實驗")
    preset = _preset(ctx)
    if a.rerun_of is not None:
        exp_id = experiments.create_rerun(ctx.conn, ctx.manager, a.rerun_of, preset)
    else:
        if a.script_id is None or a.connection_id is None or not a.scenario_ids:
            raise ToolError("需要 script_id、scenario_ids 與 connection_id（或改用 rerun_of）")
        exp_id = experiments.create_and_start(ctx.conn, ctx.manager, name=a.name, preset=preset, script_id=a.script_id,
                                              scenario_ids=a.scenario_ids, connection_id=a.connection_id)
    return {"ok": True, "experiment_id": exp_id, "note": "實驗在背景執行；用 get_experiment 查看進度"}

