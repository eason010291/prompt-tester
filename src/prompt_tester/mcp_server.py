"""② MCP server：把 agent_tools 的工具以 MCP（streamable HTTP，掛在 /mcp）提供給 Claude Code 等客戶端。

與 REST（/api/agent/tools/*）共用同一組工具與存取檢查。
"""

import hmac
import ipaddress
from collections.abc import Callable

from fastapi import FastAPI
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, agent_tools as at

INSTRUCTIONS = (
    "Prompt Tester：SillyTavern 提示詞（預設）與角色卡的測試器。一個網頁分頁一支工具：prompt（提示詞）、scenario（情境 / 角色卡）、"
    "experiment（實驗與評語）、script（腳本）、versions（版本）；每支工具用 action 指定動作，欄位用 path 定位。"
    "典型流程：experiment feedback 讀評語 → prompt read 讀預設（或 scenario read 讀角色卡）→ patch / set 修改 → diff 檢查 → commit。"
    "預設提交時若在受保護的 main 分支上，會自動建立 agent/<時間> 分支；情境的修改存成線性版本，可在網頁上 diff 與還原。"
    "experiment run 會產生 LLM 費用，需使用者在設定中開啟。"
)


def access_error(request: Request, token: str) -> str | None:
    """None = 允許。設定了 token 時必須帶 token；沒設定時只接受本機連線（並檢查 Host，防 DNS rebinding）。"""
    if token:
        given = request.headers.get("x-agent-token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return None if given and hmac.compare_digest(given, token) else "需要 agent token（config.local.toml 的 [agent] token）"
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host in ("testclient", "localhost")
    hostname = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
    if not local or hostname not in ("127.0.0.1", "localhost", "::1", "testserver"):
        return "外部 agent 接口只接受本機連線；要開放給區網請在 config.local.toml 設定 [agent] token"
    return None


def client_name(request: Request | None, session=None, default: str = "mcp") -> str:
    name = request.headers.get("x-agent-name") if request is not None else None
    if not name and session is not None:
        try:
            name = session.client_params.clientInfo.name
        except AttributeError:
            name = None
    return (name or default).strip()[:40] or default


def build(app: FastAPI, token_getter: Callable[[], str]) -> StreamableHTTPSessionManager:
    async def list_tools(ctx, params) -> types.ListToolsResult:
        settings = at.load_settings(app.state.db)
        return types.ListToolsResult(tools=[
            types.Tool(name=t.name, description=t.description(actions), inputSchema=t.schema(actions))
            for t in at.REGISTRY.values()
            if (actions := t.enabled(settings))
        ])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        conn = app.state.db
        name = client_name(ctx.request if isinstance(ctx.request, Request) else None, ctx.session)
        tctx = at.Ctx(conn=conn, author=f"agent:{name}", settings=at.load_settings(conn), manager=app.state.experiments,
                      meta={"source": "mcp", "client": name}, branch_prefix="agent")
        try:
            result = at.call(tctx, params.name, params.arguments or {})
        except at.ToolError as e:
            return types.CallToolResult(content=[types.TextContent(text=str(e))], isError=True)
        return types.CallToolResult(content=[types.TextContent(text=at.to_text(result, 200_000))])

    server = Server("prompt-tester", version=__version__, instructions=INSTRUCTIONS,
                    on_list_tools=list_tools, on_call_tool=call_tool)
    return StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)


class Endpoint:
    """掛在 /mcp 的 ASGI app：先做存取檢查，再交給 MCP session manager。"""

    def __init__(self, manager: StreamableHTTPSessionManager, token_getter: Callable[[], str]):
        self.manager = manager
        self.token_getter = token_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            err = access_error(Request(scope), self.token_getter())
            if err:
                await JSONResponse({"detail": err}, status_code=403)(scope, receive, send)
                return
        await self.manager.handle_request(scope, receive, send)
