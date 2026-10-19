# Prompt Tester

本機執行的提示詞測試器：區塊式組裝 Chat Completion 提示詞 → 依金字塔腳本批次生成 → 人工評語 → LLM 依評語修改 → 與父版本迴歸比較。預設與腳本各有類 git 的版本樹。

## 需求

- [uv](https://docs.astral.sh/uv/)（Python 3.14 由 uv 自動安裝）
- Node.js 24+

## 首次設定

```sh
uv sync
cd web && npm install && cd ..
cp config.example.toml config.local.toml   # 填入 OpenRouter API key
```

## 啟動

**一般使用**（後端直接提供建置好的前端）：

```sh
cd web && npm run build && cd ..
uv run prompt-tester
```

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
| `src/prompt_tester/` | FastAPI 後端 |
| `web/` | React + Vite 前端 |
| `tests/` | 後端測試 |
| `data/` | SQLite 資料庫（自動建立，不入版控） |
| `config.local.toml` | 本機設定與 API key（不入版控） |
