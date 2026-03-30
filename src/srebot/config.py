import logging

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from srebot.parser.filtering import FilterCondition, IgnoreRule

logger = logging.getLogger(__name__)


class MCPServerConfig(BaseModel):
    """Configuration for an external MCP server."""

    name: str = ""  # auto-populated from key if omitted
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    read_only: bool = False  # if True, only allow read-like tools
    condition: FilterCondition | None = None  # Optional rule to restrict server usage


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file="config.yml",
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = ""
    telegram_channel_id: int = 0

    # SaaS Control Plane
    saas_ws_url: str = "wss://api.srebot.site360.tech/api/v1/agent/connect"
    saas_agent_token: str = ""
    llm_response_language: str = "English"
    llm_max_iterations: int = 10  # tool-call loop guard
    bot_container_name: str = "ai-observability-bot"  # used for self-filtering in logs

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    alert_fingerprint_ttl: int = 86400  # seconds

    # MCP servers config (unified)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    # Alert ignore rules (inline in config.yml)
    ignore_rules: list[IgnoreRule] = Field(default_factory=list)

    # Logging
    log_level: str = "INFO"

    # Dry-run / debug mode — log all outgoing messages instead of sending to Telegram
    dry_run: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Enable YamlConfigSettingsSource. Order determines priority (env overrides yaml).
        yaml_source = YamlConfigSettingsSource(settings_cls)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            yaml_source,
            file_secret_settings,
        )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log level: {v}")
        return v


class MCPServerRegistry:
    """Registry of all configured external MCP servers."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers

    @classmethod
    def from_settings(cls, settings: Settings) -> "MCPServerRegistry":
        servers = {}
        for name, cfg in settings.mcp_servers.items():
            if not cfg.name:
                cfg.name = name
            servers[name] = cfg

        logger.info("Loaded %d MCP server(s) config: %s", len(servers), list(servers))
        return cls(servers)

    def all_configs(self) -> list[MCPServerConfig]:
        return list(self._servers.values())


# Module-level singletons (initialized once in main.py)
_settings: Settings | None = None
_mcp_registry: MCPServerRegistry | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        # Pydantic logs a warning if config.yml is missing, which is fine.
        _settings = Settings()
    return _settings


def get_mcp_registry() -> MCPServerRegistry:
    global _mcp_registry
    if _mcp_registry is None:
        s = get_settings()
        _mcp_registry = MCPServerRegistry.from_settings(s)
    return _mcp_registry
