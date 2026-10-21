# Prompt Tester

本機執行的提示詞測試器：區塊式組裝 Chat Completion 提示詞 → 依金字塔腳本批次生成 → 人工評語 → LLM 依評語修改 → 與父版本迴歸比較。預設與腳本各有類 git 的版本樹。組裝規則對照 SillyTavern 1.18.0，可直接匯入 / 匯出酒館預設。

## 目前進度

| 里程碑 | 內容 | 狀態 |
|--------|------|------|
| M0 | 專案骨架（FastAPI + SQLite + React/Vite） | ✅ |
| M1 | 區塊組裝、巨集、深度注入、`messages[]` 即時預覽 | ✅ |
| M7（部分） | ST 預設匯入 / 匯出、角色卡（JSON / PNG）、世界書觸發、範例對話 | ✅ |
| M8（部分） | 正則與訊息渲染（與 ST 逐字比對一致） | ✅ |
| M2 | OpenRouter 連線、情境管理、串流生成與紀錄 | ✅ |
| M3 | 預設版本樹（commit、分支、標籤、diff、cherry-pick） | ✅ |
| M4 | LLM 扮演 {{user}} 的多回合試跑；腳本繼承樹；金字塔實驗（多 seed、溫度掃描、晉級、評審預選、重跑對照） | ✅ |
| M5 | 評語（自由文字 + 分數解析）、試跑並排比較、盲評、版本平均分 | ✅ |
| M6 | LLM 依評語修改：內建 tool-calling 迴圈、MCP（HTTP / stdio）、REST 工具；一鍵修改 → 提交 → 重跑 | ✅ |
| 實驗操作 | 分層重試（單則 / 分支 / 階段，含原因標籤與重試率統計）、手動接管使用者輸入、卡住偵測與續跑、迴歸試算 → 驗證輪次 | ✅ |

## 使用流程

1. **提示詞**：匯入 ST 預設或手動組裝區塊，右側即時預覽送出的 `messages[]`；改好後在上方「提交」。
2. **情境**：匯入角色卡或手填開場、歷史、使用者人設與目標；可先「預檢」2 回合。
3. **腳本**：設定金字塔各階段的回合數、分支數、溫度掃描與晉級方式（子腳本只存覆寫）。
4. **實驗**：選腳本、情境與連線開跑；每階段結束後挑選晉級的分支，對回覆寫評語（「屬性：n/10」會解析成分數），不滿意的回覆可重試。
5. **修改與迴歸**：「依評語修改」讓 LLM 改預設並提交到新分支；在有問題的回覆上「🔍 迴歸試算」比較新舊版本，滿意後開「驗證輪次」只重跑相關分支。
6. **版本樹**：查看各版本的平均分、diff，切換或 cherry-pick 區塊。

## 需求

- [uv](https://docs.astral.sh/uv/)（Python 3.14 由 uv 自動安裝）
- Node.js 24+

## 首次設定

```sh
uv sync
cd web && npm install && cd ..
cp config.example.toml config.local.toml   # 或啟動後在「設定」頁填入 OpenRouter API key
```

## 啟動

**一般使用**（後端直接提供建置好的前端）：

```sh
cd web && npm run build && cd ..
uv run prompt-tester
```

Windows 可直接執行 `build_n_start.bat`（任何目錄皆可；額外參數轉給 prompt-tester，例如 `--port 8001`）。

開啟 <http://127.0.0.1:8000>。

**開發**（兩個終端機，前端熱更新，`/api` 由 Vite 代理到後端）：

```sh
uv run prompt-tester --reload
cd web && npm run dev                      # http://localhost:5173
```

## 外部 agent 接入（MCP / REST）

修改預設的工具（讀評語、讀 / 改區塊、預覽、提交…）同時以三種方式提供，規則相同：只能改工作區，提交到受保護的 main 時會自動改建 `agent/…` 分支。

```sh
# Claude Code（伺服器需在執行中）
claude mcp add --transport http prompt-tester http://127.0.0.1:8000/mcp
# 只支援 stdio 的客戶端（轉發到執行中的伺服器）
uv run --directory <專案資料夾> python -m prompt_tester.mcp_stdio   # 環境變數 PROMPT_TESTER_URL
# REST：POST /api/agent/tools/<工具名稱>，文件見 http://127.0.0.1:8000/docs
```

預設只接受本機連線。要開放給區網，在 `config.local.toml` 加上 `[agent]` 區段的 `token = "..."`，客戶端以 `Authorization: Bearer <token>` 或 `X-Agent-Token` 標頭帶入。會產生費用的 `run_experiment` 需在設定頁開啟。

## 測試

```sh
uv run pytest
cd web && npm run lint && npm run build
```

## 目錄

| 路徑 | 內容 |
|------|------|
| `src/prompt_tester/preset.py` | 預設資料模型（欄位沿用 ST 命名） |
| `src/prompt_tester/macros.py` | ST 巨集子集（對照 1.18.0 新巨集引擎） |
| `src/prompt_tester/build.py` | 組裝器（對照 ST `openai.js` / `prompt-converters.js`） |
| `src/prompt_tester/st_preset.py` | ST 預設匯入 / 匯出 |
| `src/prompt_tester/st_card.py` | ST 角色卡讀取 |
| `src/prompt_tester/worldinfo.py` | 世界書觸發 |
| `src/prompt_tester/vcs.py`、`workspace.py` | 預設版本庫（blob / tree / commit / ref、diff）、工作區提交 |
| `src/prompt_tester/runner.py` | 多回合試跑（LLM 扮演使用者、回放、接續與重試） |
| `src/prompt_tester/llm.py` | OpenAI 相容 client（串流、重試、卡住偵測、tool calling） |
| `src/prompt_tester/providers.py` | 供應商金鑰與模型目錄 |
| `src/prompt_tester/scripts.py`、`experiments.py` | 腳本繼承樹、金字塔實驗 |
| `src/prompt_tester/feedback.py` | 評語與分數 |
| `src/prompt_tester/agent_tools.py`、`modifier.py` | LLM 修改工具與內建迴圈 |
| `src/prompt_tester/mcp_server.py`、`mcp_stdio.py` | MCP 接口 |
| `src/prompt_tester/routes/` | API 路由 |
| `web/` | React + Vite 前端（`lib/st/` 為 ST 正則與訊息渲染的移植） |
| `tests/` | 後端測試 |
| `data/` | SQLite 資料庫（自動建立，不入版控） |
| `config.local.toml` | 本機設定與 API key（不入版控） |
| `doc/` | 工作文件（設計、進度；不入版控） |

## 分支

- `main`：穩定版
- `dev`：開發整合
- `feature/<里程碑-主題>`：從 `dev` 分出，完成並驗證後合併回 `dev`
