"""Configuration — settings and cluster registry."""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class MCPServerConfig(BaseModel):
    """Configuration for an external MCP server."""

    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] | None = None
    read_only: bool = False  # if True, only allow read-like tools (block create/delete/update)


class MCPServerRegistry:
    """Registry of all configured external MCP servers."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers

    @classmethod
    def from_yaml(cls, path: str | Path) -> "MCPServerRegistry":
        path = Path(path)
        if not path.exists():
            logger.warning("mcp_servers.yml not found at %s — no external servers configured", path)
            return cls({})

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        servers: dict[str, MCPServerConfig] = {}
        for name, cfg in (data.get("mcp_servers") or {}).items():
            servers[name] = MCPServerConfig(
                name=name,
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=cfg.get("env"),
                read_only=cfg.get("read_only", False),
            )
        logger.info("Loaded %d MCP server(s) config: %s", len(servers), list(servers))
        return cls(servers)

    def all_configs(self) -> list[MCPServerConfig]:
        return list(self._servers.values())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    telegram_channel_id: int

    # LLM — any OpenAI-compatible provider
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str
    llm_model: str = "gpt-4o"
    llm_response_language: str = "English"
    llm_max_iterations: int = 10  # tool-call loop guard

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    alert_fingerprint_ttl: int = 86400  # seconds

    # MCP servers config
    mcp_servers_config_path: str = "mcp_servers.yml"

    # Alert ignore rules
    alert_ignore_rules_path: str = "ignore_rules.yml"

    # Logging
    log_level: str = "INFO"

    # Dry-run / debug mode — log all outgoing messages instead of sending to Telegram
    dry_run: bool = False

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log level: {v}")
        return v


# Module-level singletons (initialized once in main.py)
_settings: Settings | None = None
_mcp_registry: MCPServerRegistry | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_mcp_registry() -> MCPServerRegistry:
    global _mcp_registry
    if _mcp_registry is None:
        s = get_settings()
        _mcp_registry = MCPServerRegistry.from_yaml(s.mcp_servers_config_path)
    return _mcp_registry
