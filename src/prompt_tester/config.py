"""本機設定：讀取 config.local.toml（不入版控），缺檔時使用預設值。"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.local.toml"


@dataclass(frozen=True)
class ProviderKey:
    name: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = PROJECT_ROOT / "data"
    web_dist: Path = PROJECT_ROOT / "web" / "dist"
    providers: dict[str, ProviderKey] = field(default_factory=dict)
    # 外部 agent（MCP / REST 工具）的存取 token；空字串時只接受本機連線
    agent_token: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prompt_tester.db"


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("PROMPT_TESTER_CONFIG", DEFAULT_CONFIG_PATH))
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    server = raw.get("server", {})
    providers = {
        name: ProviderKey(name=name, base_url=p["base_url"], api_key=p.get("api_key", ""))
        for name, p in raw.get("providers", {}).items()
    }
    data_dir = Path(server["data_dir"]) if "data_dir" in server else Settings.data_dir
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return Settings(
        host=server.get("host", Settings.host),
        port=server.get("port", Settings.port),
        data_dir=data_dir,
        providers=providers,
        agent_token=raw.get("agent", {}).get("token", ""),
    )
