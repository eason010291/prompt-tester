"""預設（Preset）資料模型。

欄位命名盡量沿用 SillyTavern Chat Completion 預設，方便 M7 匯入 / 匯出：
- 區塊列表的順序 = ST `prompt_order`（character_id 100001）的順序
- `enabled` 存在區塊上（ST 存在 prompt_order 裡，匯入時轉換）
"""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]
BlockKind = Literal["content", "marker", "divider"]
# ST 內建 marker。M1 只有 chatHistory 有內容來源，其餘在 M7（角色卡 / 世界書）接上前組裝為空。
Marker = Literal[
    "chatHistory",
    "dialogueExamples",
    "worldInfoBefore",
    "worldInfoAfter",
    "charDescription",
    "charPersonality",
    "scenario",
    "personaDescription",
]
PostProcessing = Literal["", "merge", "semi", "strict", "single"]

# 對應 ST injection_position：0 = 依列表位置（相對），1 = 依深度插入聊天歷史（In-Chat）
POSITION_RELATIVE = 0
POSITION_IN_CHAT = 1


def new_id() -> str:
    return str(uuid4())


class Block(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = ""
    kind: BlockKind = "content"
    marker: Marker | None = None
    role: Role = "system"
    content: str = ""
    enabled: bool = True
    injection_position: int = POSITION_RELATIVE
    injection_depth: int = 4
    injection_order: int = 100
    # 互斥變體群組：同群組內最多一個啟用（由前端維護，組裝時不強制）
    group: str = ""
    # 前端用：卡片是否摺疊
    collapsed: bool = False
    # ST 專屬欄位（system_prompt、forbid_overrides、injection_trigger…），匯出時寫回
    st: dict = Field(default_factory=dict)


class Params(BaseModel):
    """送給 API 的取樣參數，以及組裝時的 token 預算。"""

    temperature: float = 1.0
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    max_tokens: int = 4000  # ST: openai_max_tokens
    max_context: int = 60000  # ST: openai_max_context


class Options(BaseModel):
    squash_system_messages: bool = True
    post_processing: PostProcessing = ""
    # ST 預設值；聊天歷史最前面的分隔訊息，留空則不加
    new_chat_prompt: str = "[Start a new Chat]"
    # ST config.yaml promptPlaceholder；strict 後處理補位用
    prompt_placeholder: str = "[Start a new chat]"
    # 每個範例對話區塊前的分隔訊息
    new_example_chat_prompt: str = "[Example Chat]"
    # 角色卡欄位的包裝格式（ST 預設值）；wi_format 以 {0} 代表世界書內容
    wi_format: str = "{0}"
    scenario_format: str = "{{scenario}}"
    personality_format: str = "{{personality}}"
    # ST power_user：角色卡的 system_prompt / post_history_instructions 覆寫 main / jailbreak
    prefer_character_prompt: bool = True
    prefer_character_jailbreak: bool = True
    # 世界書（ST 全新安裝預設：遞迴與全字比對開啟）
    wi_scan_depth: int = 2
    wi_budget_percent: int = 25
    wi_budget_cap: int = 0
    wi_recursive: bool = True
    wi_case_sensitive: bool = False
    wi_match_whole_words: bool = True
    wi_include_names: bool = True


class Preset(BaseModel):
    name: str = "未命名預設"
    blocks: list[Block] = Field(default_factory=list)
    params: Params = Field(default_factory=Params)
    options: Options = Field(default_factory=Options)
    # 匯入時無法辨識的欄位，原樣保留以便匯出
    extra: dict = Field(default_factory=dict)


class HistoryMessage(BaseModel):
    role: Role
    content: str
    name: str | None = None


class PreviewContext(BaseModel):
    """提示詞頁面目前選用的情境與本次輸入。"""

    scenario_id: int | None = None
    seed: int = 1
    # 本次的使用者輸入，附加在情境歷史最後；空字串表示不附加
    user_input: str = ""


class LegacyPreviewContext(BaseModel):
    """M1 存在工作區裡的內嵌情境，載入時轉成正式情境。"""

    char: str = "角色"
    user: str = "使用者"
    seed: int = 1
    history: list[HistoryMessage] = Field(default_factory=list)


def starter_preset() -> Preset:
    """首次開啟時的範例預設，示範各種區塊類型。"""
    return Preset(
        name="範例預設",
        blocks=[
            Block(name="===主要指示===", kind="divider", enabled=False),
            Block(name="Main Prompt", content="你是 {{char}}，正在與 {{user}} 進行角色扮演。"),
            Block(name="文風", content="{{// 這行是註解，不會送出}}以繁體中文寫作，避免比喻堆疊。"),
            Block(name="聊天歷史", kind="marker", marker="chatHistory"),
            Block(name="===輸出格式===", kind="divider", enabled=False),
            Block(name="輸出格式", content="回覆分成【正文】與【狀態欄】兩段。"),
            Block(
                name="深度注入範例",
                content="（提醒：保持 {{char}} 的語氣。）",
                injection_position=POSITION_IN_CHAT,
                injection_depth=1,
                enabled=False,
            ),
        ],
    )


