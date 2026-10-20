"""M2：情境（Scenario）與連線（Connection）。"""

from typing import Literal

from pydantic import BaseModel, Field

from .preset import HistoryMessage

ReasoningEffort = Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


class LoreEntry(BaseModel):
    """世界書條目（ST 內部 WI 欄位命名，換算規則見 worldinfo.py）。"""

    uid: int
    comment: str = ""
    keys: list[str] = Field(default_factory=list)
    secondary_keys: list[str] = Field(default_factory=list)
    content: str = ""
    constant: bool = False
    selective: bool = True
    selective_logic: int = 0
    enabled: bool = True
    order: int = 100
    position: int = 0
    depth: int = 4
    role: int = 0
    probability: int = 100
    use_probability: bool = True
    scan_depth: int | None = None
    case_sensitive: bool | None = None
    match_whole_words: bool | None = None
    exclude_recursion: bool = False
    prevent_recursion: bool = False
    delay_until_recursion: int = 0
    group: str = ""
    group_override: bool = False
    group_weight: int = 100
    use_group_scoring: bool | None = None
    ignore_budget: bool = False
    outlet_name: str = ""
    triggers: list[str] = Field(default_factory=list)
    # 聊天訊息數少於此值時不觸發（ST delay；不需聊天狀態即可判斷）
    delay: int | None = None
    # 額外掃描來源：character_description、character_personality、scenario、persona_description、creator_notes、character_depth_prompt
    match_sources: list[str] = Field(default_factory=list)


class Lorebook(BaseModel):
    name: str = ""
    entries: list[LoreEntry] = Field(default_factory=list)
    scan_depth: int | None = None
    token_budget: int | None = None
    recursive_scanning: bool | None = None


class DepthPrompt(BaseModel):
    prompt: str = ""
    depth: int = 4
    role: Literal["system", "user", "assistant"] = "system"


class CharacterCard(BaseModel):
    spec: str = "chara_card_v3"
    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator_notes: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: list[str] = Field(default_factory=list)
    depth_prompt: DepthPrompt = Field(default_factory=DepthPrompt)
    regex_scripts: list[dict] = Field(default_factory=list)
    book: Lorebook | None = None
    # 原始角色卡 JSON，供匯出時保留未知欄位
    raw: dict = Field(default_factory=dict)


class Scenario(BaseModel):
    """一筆測試輸入。開場白視為第一則 assistant 訊息，之後接既有歷史。"""

    id: int | None = None
    name: str = "新情境"
    char: str = "角色"
    user: str = "使用者"
    # 其他具名值，可在區塊中以 {{名稱}} 引用（M7 起也承接角色卡欄位）
    vars: dict[str, str] = Field(default_factory=dict)
    opening: str = ""
    history: list[HistoryMessage] = Field(default_factory=list)
    # 逐回合的使用者輸入；單次生成預設帶入第一則，M4 多回合依序使用
    user_turns: list[str] = Field(default_factory=list)
    notes: str = ""
    # 使用者 persona 描述（填入 personaDescription marker）
    persona: str = ""
    # 匯入的角色卡；有值時填入 charDescription / charPersonality / scenario / dialogueExamples / 世界書等 marker
    card: CharacterCard | None = None
    updated_at: str | None = None

    def names(self) -> dict[str, str]:
        return {**self.vars, "char": self.char, "user": self.user}

    def chat(self, user_input: str = "") -> list[HistoryMessage]:
        msgs = []
        if self.opening:
            msgs.append(HistoryMessage(role="assistant", content=self.opening))
        msgs += self.history
        if user_input:
            msgs.append(HistoryMessage(role="user", content=user_input))
        return msgs


class Routing(BaseModel):
    """OpenRouter provider 路由。空值代表不送該欄位，由 OpenRouter 決定。"""

    order: list[str] = Field(default_factory=list)
    allow_fallbacks: bool = True
    only: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)
    quantizations: list[str] = Field(default_factory=list)

    def to_body(self) -> dict | None:
        body: dict = {}
        for key in ("order", "only", "ignore", "quantizations"):
            if value := getattr(self, key):
                body[key] = value
        if not self.allow_fallbacks:
            body["allow_fallbacks"] = False
        return body or None


class Connection(BaseModel):
    id: int | None = None
    name: str = "新連線"
    provider: str = "openrouter"
    model: str = ""
    routing: Routing = Field(default_factory=Routing)
    reasoning_effort: ReasoningEffort = ""
    send_seed: bool = True
    # 直接合併進 request body 的額外欄位（進階）
    extra_body: dict = Field(default_factory=dict)
