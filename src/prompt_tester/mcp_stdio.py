"""② MCP（stdio）：給只支援 stdio 的客戶端（例如 Claude Desktop）。

不直接開資料庫，而是轉發到執行中的 Prompt Tester 的 REST 工具（/api/agent/tools），
因此網頁畫面與實驗的背景執行都維持在同一個程序。

    uv run python -m prompt_tester.mcp_stdio
環境變數：PROMPT_TESTER_URL（預設 http://127.0.0.1:8000）、PROMPT_TESTER_AGENT_TOKEN、PROMPT_TESTER_AGENT_NAME
"""

import json
import os

import anyio
import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import __version__
from .mcp_server import INSTRUCTIONS


def main() -> None:
    base = os.environ.get("PROMPT_TESTER_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {"X-Agent-Name": os.environ.get("PROMPT_TESTER_AGENT_NAME", "mcp-stdio")}
    if token := os.environ.get("PROMPT_TESTER_AGENT_TOKEN"):
        headers["X-Agent-Token"] = token

    async def run() -> None:
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=120) as http:

            async def list_tools(ctx, params) -> types.ListToolsResult:
                r = await http.get("/api/agent/tools")
                r.raise_for_status()
                return types.ListToolsResult(tools=[
                    types.Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"])
                    for t in r.json()
                    if t["enabled"]
                ])

            async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
                try:
                    r = await http.post(f"/api/agent/tools/{params.name}", json=params.arguments or {})
                except httpx.HTTPError as e:
                    return types.CallToolResult(content=[types.TextContent(text=f"連不到 Prompt Tester（{base}）：{e}")], isError=True)
                if r.status_code >= 400:
                    try:
                        detail = r.json().get("detail")
                    except ValueError:
                        detail = r.text
                    return types.CallToolResult(content=[types.TextContent(text=str(detail))], isError=True)
                return types.CallToolResult(content=[types.TextContent(text=json.dumps(r.json()["result"], ensure_ascii=False))])

            server = Server("prompt-tester", version=__version__, instructions=INSTRUCTIONS,
                            on_list_tools=list_tools, on_call_tool=call_tool)
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
