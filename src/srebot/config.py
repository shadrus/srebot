import logging

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from srebot.parser.alert_parser import Alert
from srebot.parser.filtering import FilterCondition, IgnoreRule

logger = logging.getLogger(__name__)


class MCPServerConfig(BaseModel):
    """Configuration for an external MCP server."""

    name: str = ""  # auto-populated from key if omitted
    url: str
    transport: str = "sse"  # "sse" or "http" (Streamable HTTP)
    read_only: bool = False  # if True, only allow read-like tools
    condition: FilterCondition | None = None  # Optional rule to restrict server usage


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        yaml_file="config.yml",
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = ""
    telegram_channel_id: int = 0

    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channel_id: str = ""

    # Discord
    discord_bot_token: str = ""
    discord_channel_id: int = 0

    # Time Messenger
    time_base_url: str = ""
    time_token: str = ""
    time_channel_id: str = ""

    # SaaS Control Plane
    saas_ws_url: str = "wss://api.srebot.site360.tech/api/v1/agent/connect"
    saas_agent_token: str = ""
    llm_response_language: str = "English"
    bot_container_name: str = "srebot"  # used for self-filtering in logs

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    alert_fingerprint_ttl: int = 86400  # seconds

    # Follow-up thread settings
    followup_max_turns: int = 5  # Deprecated alias for per-user incident turns
    followup_user_max_turns: int | None = None  # Max turns per user and incident
    followup_incident_max_turns: int = 20  # Max turns across all users for one incident
    followup_ttl: int = 43200  # Seconds follow-up context window stays open (12 h)
    followup_user_cooldown_sec: int = 10  # Min seconds between follow-ups per user
    alert_analysis_timeout: int = 600  # seconds
    followup_analysis_timeout: int = 300  # seconds

    # MCP connection retry settings (for sidecar startup races)
    mcp_connect_retries: int = 5  # Max connection attempts per MCP server
    mcp_connect_retry_delay: float = 3.0  # Base delay in seconds (doubles each retry)

    # MCP servers config (unified)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    # Alert ignore rules (inline in config.yml)
    ignore_rules: list[IgnoreRule] = Field(default_factory=list)

    # Logging
    log_level: str = "INFO"

    # Dry-run / debug mode — log all outgoing messages instead of sending to Telegram
    dry_run: bool = False

    # If false, bots will only run analysis when explicitly asked by the user in a reply
    auto_analyze_alerts: bool = True

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

    @field_validator(
        "followup_max_turns",
        "followup_user_max_turns",
        "followup_incident_max_turns",
        "followup_ttl",
        "followup_user_cooldown_sec",
    )
    @classmethod
    def validate_positive_followup_setting(cls, v: int | None) -> int | None:
        """Reject non-positive follow-up quota and lifetime settings."""
        if v is not None and v <= 0:
            raise ValueError("follow-up settings must be positive")
        return v

    @property
    def effective_followup_user_max_turns(self) -> int:
        """Return the new per-user limit or the legacy setting when unset."""
        return self.followup_user_max_turns or self.followup_max_turns


class MCPServerRegistry:
    """Registry of all configured external MCP servers."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers

    @classmethod
    def from_settings(cls, settings: Settings) -> MCPServerRegistry:
        servers = {}
        for name, cfg in settings.mcp_servers.items():
            if not cfg.name:
                cfg.name = name
            servers[name] = cfg

        logger.info("Loaded %d MCP server(s) config: %s", len(servers), list(servers))
        return cls(servers)

    def all_configs(self) -> list[MCPServerConfig]:
        return list(self._servers.values())

    def allowed_server_names(self, alert: Alert) -> list[str]:
        """Return MCP server names whose conditions allow the given alert.

        Args:
            alert: Validated primary alert used for request routing.

        Returns:
            Configured server names allowed for this alert in registry order.
        """
        allowed_servers = []
        for server in self.all_configs():
            if server.condition is None or server.condition.matches(alert):
                allowed_servers.append(server.name)
            else:
                logger.debug(
                    "Server %r blocked for group %s by condition",
                    server.name,
                    alert.alertname,
                )
        return allowed_servers


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
