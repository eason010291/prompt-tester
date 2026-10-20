"""供應商（API 端點 + 金鑰）與模型目錄。

金鑰只存在 config.local.toml 或環境變數（`<NAME>_API_KEY`，例如 OPENROUTER_API_KEY），不進資料庫。
從網頁設定金鑰時會改寫 config.local.toml（檔案中的註解不會保留）。
"""

import os
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import DEFAULT_CONFIG_PATH

DEFAULT_PROVIDERS = {"openrouter": "https://openrouter.ai/api/v1"}
CATALOG_TTL = 3600


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    key_source: str  # "config" | "env" | ""

    @property
    def is_openrouter(self) -> bool:
        return "openrouter.ai" in self.base_url


def config_path() -> Path:
    return Path(os.environ.get("PROMPT_TESTER_CONFIG", DEFAULT_CONFIG_PATH))


def _read_config() -> dict:
    path = config_path()
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def list_providers() -> list[Provider]:
    raw = _read_config().get("providers", {})
    names = dict.fromkeys([*DEFAULT_PROVIDERS, *raw])
    return [get_provider(n) for n in names]


def get_provider(name: str) -> Provider:
    cfg = _read_config().get("providers", {}).get(name, {})
    base_url = (cfg.get("base_url") or DEFAULT_PROVIDERS.get(name, "")).rstrip("/")
    if cfg.get("api_key"):
        return Provider(name, base_url, cfg["api_key"], "config")
    env_key = os.environ.get(f"{name.upper()}_API_KEY", "")
    return Provider(name, base_url, env_key, "env" if env_key else "")


def save_provider(name: str, base_url: str, api_key: str | None) -> Provider:
    """更新 config.local.toml 中的 [providers.<name>]。api_key 為 None 表示不變更。"""
    data = _read_config()
    providers = data.setdefault("providers", {})
    entry = providers.setdefault(name, {})
    entry["base_url"] = base_url.rstrip("/")
    if api_key is not None:
        entry["api_key"] = api_key
    config_path().write_text(_dump_toml(data), encoding="utf-8")
    return get_provider(name)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    escaped = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _dump_toml(data: dict) -> str:
    """本專案設定檔的結構只有一到兩層表格，這裡只處理這些情況。"""
    lines: list[str] = ["# 由 Prompt Tester 寫入；可手動編輯。"]
    for section, body in data.items():
        if not isinstance(body, dict):
            lines.append(f"{section} = {_toml_value(body)}")
            continue
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        if scalars or not tables:
            lines += ["", f"[{section}]", *(f"{k} = {_toml_value(v)}" for k, v in scalars.items())]
        for name, sub in tables.items():
            lines += ["", f"[{section}.{name}]", *(f"{k} = {_toml_value(v)}" for k, v in sub.items())]
    return "\n".join(lines) + "\n"


# ---- 模型目錄（OpenRouter /models、/models/{id}/endpoints；其他 OpenAI 相容端點只有 /models） ----

_cache: dict[str, tuple[float, object]] = {}


def _per_million(v) -> float | None:
    try:
        return round(float(v) * 1e6, 4)
    except (TypeError, ValueError):
        return None


def _pricing(p: dict) -> dict:
    return {
        "prompt": _per_million(p.get("prompt")),
        "completion": _per_million(p.get("completion")),
        "cache_read": _per_million(p.get("input_cache_read")),
    }


def _headers(provider: Provider) -> dict:
    return {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}


async def fetch_models(client: httpx.AsyncClient, provider: Provider, refresh: bool = False) -> list[dict]:
    key = f"models:{provider.base_url}"
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CATALOG_TTL:
        return hit[1]  # type: ignore[return-value]
    r = await client.get(f"{provider.base_url}/models", headers=_headers(provider), timeout=30)
    r.raise_for_status()
    models = [
        {
            "id": m["id"],
            "name": m.get("name") or m["id"],
            "created": m.get("created"),
            "context_length": m.get("context_length"),
            "pricing": _pricing(m.get("pricing") or {}),
            "supported_parameters": m.get("supported_parameters") or [],
        }
        for m in r.json().get("data", [])
    ]
    models.sort(key=lambda m: -(m["created"] or 0))
    _cache[key] = (time.time(), models)
    return models


async def fetch_endpoints(client: httpx.AsyncClient, provider: Provider, model_id: str) -> list[dict]:
    if not provider.is_openrouter:
        return []
    key = f"endpoints:{model_id}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CATALOG_TTL:
        return hit[1]  # type: ignore[return-value]
    r = await client.get(f"{provider.base_url}/models/{model_id}/endpoints", headers=_headers(provider), timeout=30)
    r.raise_for_status()
    eps = [
        {
            "provider": e.get("provider_name"),
            "tag": e.get("tag"),
            "quantization": e.get("quantization"),
            "context_length": e.get("context_length"),
            "max_completion_tokens": e.get("max_completion_tokens"),
            "pricing": _pricing(e.get("pricing") or {}),
            "uptime": e.get("uptime_last_30m"),
            "status": e.get("status"),
            "supported_parameters": e.get("supported_parameters") or [],
        }
        for e in r.json().get("data", {}).get("endpoints", [])
    ]
    eps.sort(key=lambda e: (e["pricing"]["prompt"] or 0, e["pricing"]["completion"] or 0))
    _cache[key] = (time.time(), eps)
    return eps


def cached_model(provider: Provider, model_id: str) -> dict | None:
    hit = _cache.get(f"models:{provider.base_url}")
    if not hit:
        return None
    return next((m for m in hit[1] if m["id"] == model_id), None)  # type: ignore[union-attr]
