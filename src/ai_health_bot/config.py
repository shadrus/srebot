"""Configuration — settings and cluster registry."""

import logging
from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ClusterConfig:
    """Configuration for a single monitored cluster."""

    def __init__(
        self,
        name: str,
        prometheus_url: str,
        elasticsearch_url: str | None = None,
        elasticsearch_index_pattern: str = "logs-*",
    ) -> None:
        self.name = name
        self.prometheus_url = prometheus_url.rstrip("/")
        self.elasticsearch_url = elasticsearch_url.rstrip("/") if elasticsearch_url else None
        self.elasticsearch_index_pattern = elasticsearch_index_pattern

    def __repr__(self) -> str:
        return f"ClusterConfig(name={self.name!r}, prometheus={self.prometheus_url!r})"


class ClusterRegistry:
    """Registry of all configured clusters, keyed by cluster label value."""

    def __init__(self, clusters: dict[str, ClusterConfig]) -> None:
        self._clusters = clusters

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ClusterRegistry":
        path = Path(path)
        if not path.exists():
            logger.warning("clusters.yml not found at %s — no clusters configured", path)
            return cls({})

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        clusters: dict[str, ClusterConfig] = {}
        for name, cfg in (data.get("clusters") or {}).items():
            clusters[name] = ClusterConfig(
                name=name,
                prometheus_url=cfg.get("prometheus_url", ""),
                elasticsearch_url=cfg.get("elasticsearch_url"),
                elasticsearch_index_pattern=cfg.get("elasticsearch_index_pattern", "logs-*"),
            )
        logger.info("Loaded %d cluster(s): %s", len(clusters), list(clusters))
        return cls(clusters)

    def get(self, cluster_name: str) -> ClusterConfig | None:
        return self._clusters.get(cluster_name)

    def all_names(self) -> list[str]:
        return list(self._clusters.keys())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    telegram_channel_id: int

    # LLM — any OpenAI-compatible provider
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str
    llm_model: str = "gpt-4o"
    llm_max_iterations: int = 10  # tool-call loop guard

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    alert_fingerprint_ttl: int = 86400  # seconds

    # Clusters config
    clusters_config_path: str = "clusters.yml"

    # Logging
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log level: {v}")
        return v


# Module-level singletons (initialized once in main.py)
_settings: Settings | None = None
_cluster_registry: ClusterRegistry | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_cluster_registry() -> ClusterRegistry:
    global _cluster_registry
    if _cluster_registry is None:
        s = get_settings()
        _cluster_registry = ClusterRegistry.from_yaml(s.clusters_config_path)
    return _cluster_registry
