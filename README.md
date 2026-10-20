# Prompt Tester

本機執行的提示詞測試器：區塊式組裝 Chat Completion 提示詞 → 依金字塔腳本批次生成 → 人工評語 → LLM 依評語修改 → 與父版本迴歸比較。預設與腳本各有類 git 的版本樹。組裝規則對照 SillyTavern 1.18.0，可直接匯入 / 匯出酒館預設。

## 目前進度

| 里程碑 | 內容 | 狀態 |
|--------|------|------|
| M0 | 專案骨架（FastAPI + SQLite + React/Vite） | ✅ |
| M1 | 區塊組裝、巨集、深度注入、`messages[]` 即時預覽 | ✅ |
| M7（部分） | SillyTavern Chat Completion 預設匯入 / 匯出 | ✅ |
| M2 | OpenRouter 連線、情境管理、串流生成與紀錄 | ✅ |
| M3 | 預設版本樹（commit、分支、diff） | 下一步 |
| M4–M6 | 金字塔批次、評語迴歸、LLM 修改（內建迴圈 / MCP / REST） | 規劃中 |

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
| `src/prompt_tester/llm.py` | OpenAI 相容串流 client |
| `src/prompt_tester/providers.py` | 供應商金鑰與模型目錄 |
| `src/prompt_tester/routes/` | API 路由 |
| `web/` | React + Vite 前端 |
| `tests/` | 後端測試 |
| `data/` | SQLite 資料庫（自動建立，不入版控） |
| `config.local.toml` | 本機設定與 API key（不入版控） |
| `doc/` | 工作文件（設計、進度；不入版控） |

## 分支

- `main`：穩定版
- `dev`：開發整合
- `feature/<里程碑-主題>`：從 `dev` 分出，完成並驗證後合併回 `dev`
