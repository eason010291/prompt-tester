"""M2：情境（Scenario）與連線（Connection）。"""

from typing import Literal

from pydantic import BaseModel, Field

from .preset import HistoryMessage

ReasoningEffort = Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


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
