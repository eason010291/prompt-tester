"""供應商金鑰、模型目錄、連線設定。"""

import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import providers, store
from ..entities import Connection
from .deps import get_db, get_http

router = APIRouter(prefix="/api")


class ProviderOut(BaseModel):
    name: str
    base_url: str
    has_key: bool
    key_source: str
    key_hint: str


def _out(p: providers.Provider) -> ProviderOut:
    hint = f"…{p.api_key[-4:]}" if len(p.api_key) >= 8 else ("已設定" if p.api_key else "")
    return ProviderOut(name=p.name, base_url=p.base_url, has_key=bool(p.api_key), key_source=p.key_source, key_hint=hint)


@router.get("/providers")
def list_providers() -> list[ProviderOut]:
    return [_out(p) for p in providers.list_providers()]


class ProviderIn(BaseModel):
    base_url: str
    # None = 不變更；空字串 = 清除
    api_key: str | None = None


@router.put("/providers/{name}")
def save_provider(name: str, body: ProviderIn) -> ProviderOut:
    if not body.base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url 必須是 http(s) 網址")
    return _out(providers.save_provider(name, body.base_url, body.api_key))


@router.get("/providers/{name}/models")
async def list_models(name: str, refresh: bool = False, http: httpx.AsyncClient = Depends(get_http)) -> list[dict]:
    try:
        return await providers.fetch_models(http, providers.get_provider(name), refresh)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"無法取得模型列表：{e}") from e


@router.get("/providers/{name}/endpoints")
async def list_endpoints(name: str, model: str, http: httpx.AsyncClient = Depends(get_http)) -> list[dict]:
    try:
        return await providers.fetch_endpoints(http, providers.get_provider(name), model)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"無法取得供應商列表：{e}") from e


@router.get("/connections")
def list_connections(conn: sqlite3.Connection = Depends(get_db)) -> list[Connection]:
    return store.list_connections(conn)


@router.post("/connections")
def create_connection(c: Connection, conn: sqlite3.Connection = Depends(get_db)) -> Connection:
    c.id = None
    return store.save_connection(conn, c)


@router.put("/connections/{cid}")
def update_connection(cid: int, c: Connection, conn: sqlite3.Connection = Depends(get_db)) -> Connection:
    c.id = cid
    try:
        return store.save_connection(conn, c)
    except KeyError:
        raise HTTPException(404, f"找不到連線 #{cid}") from None


@router.delete("/connections/{cid}", status_code=204)
def delete_connection(cid: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    store.delete_connection(conn, cid)
