from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db, store
from .config import Settings, load_settings
from .routes import generate, prompt, runs, scenarios, settings as settings_routes, vcs as vcs_routes


def create_app(settings: Settings | None = None, http_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """http_transport：測試時注入假的 LLM 端點。"""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        db.migrate(conn)
        store.mark_stale_running(conn)
        prompt.migrate_workspace(conn)
        app.state.db = conn
        async with httpx.AsyncClient(transport=http_transport, timeout=30) as http:
            app.state.http = http
            yield
        conn.close()

    app = FastAPI(title="Prompt Tester", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.get("/api/health")
    def health(request: Request) -> dict:
        conn = request.app.state.db
        return {
            "status": "ok",
            "version": __version__,
            "schema_version": db.schema_version(conn),
            "providers": sorted(settings.providers),
        }

    app.include_router(prompt.router)
    app.include_router(scenarios.router)
    app.include_router(settings_routes.router)
    app.include_router(generate.router)
    app.include_router(vcs_routes.router)
    app.include_router(runs.router)
    _mount_frontend(app, settings)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """有建置好的前端就由後端直接提供；開發時改用 Vite dev server。"""
    dist = settings.web_dist
    index = dist / "index.html"
    if not index.exists():
        return

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        file = (dist / path).resolve()
        if path and file.is_file() and file.is_relative_to(dist.resolve()):
            return FileResponse(file)
        # index.html 引用帶 hash 的資源檔，必須每次重新驗證，否則前端更新後會載入舊版
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
