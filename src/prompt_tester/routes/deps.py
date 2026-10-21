import sqlite3

import httpx
from fastapi import Request


def get_db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http
