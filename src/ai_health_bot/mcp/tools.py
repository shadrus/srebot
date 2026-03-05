"""MCP tool functions for querying Prometheus and Elasticsearch per cluster."""

import logging
from datetime import UTC, datetime

import httpx

from ai_health_bot.config import ClusterConfig, get_cluster_registry

logger = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


def _resolve_cluster(cluster_name: str) -> ClusterConfig:
    registry = get_cluster_registry()
    cfg = registry.get(cluster_name)
    if cfg is None:
        known = registry.all_names()
        raise ValueError(f"Unknown cluster {cluster_name!r}. Known clusters: {known}")
    return cfg


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Prometheus tools
# ---------------------------------------------------------------------------


async def query_prometheus(cluster: str, expr: str, time: str | None = None) -> dict:
    """
    Run an instant PromQL query against the given cluster's Prometheus.

    Args:
        cluster: Cluster name matching clusters.yml key (e.g. "google-production").
        expr: PromQL expression
              (e.g. 'kube_deployment_status_replicas_available{namespace="production"}').
        time: RFC3339 timestamp or Unix timestamp. Defaults to now.

    Returns:
        Prometheus API response dict with 'status' and 'data' fields.
    """
    cfg = _resolve_cluster(cluster)
    params: dict = {"query": expr}
    if time:
        params["time"] = time

    resp = await _get_http().get(f"{cfg.prometheus_url}/api/v1/query", params=params)
    resp.raise_for_status()
    return resp.json()


async def query_prometheus_range(
    cluster: str,
    expr: str,
    start: str,
    end: str,
    step: str = "60s",
) -> dict:
    """
    Run a range PromQL query to retrieve metric history.

    Args:
        cluster: Cluster name.
        expr: PromQL expression.
        start: Start time (RFC3339 or Unix timestamp).
        end: End time (RFC3339 or Unix timestamp).
        step: Resolution step (e.g. "60s", "5m").

    Returns:
        Prometheus range query API response.
    """
    cfg = _resolve_cluster(cluster)
    params = {"query": expr, "start": start, "end": end, "step": step}
    resp = await _get_http().get(f"{cfg.prometheus_url}/api/v1/query_range", params=params)
    resp.raise_for_status()
    return resp.json()


async def get_active_alerts(cluster: str, alertname: str | None = None) -> dict:
    """
    List active (firing) alerts from Alertmanager or Prometheus.

    Args:
        cluster: Cluster name.
        alertname: Optional filter by alert name.

    Returns:
        Dict with list of active alert objects.
    """
    cfg = _resolve_cluster(cluster)
    params: dict = {}
    if alertname:
        params["filter"] = f'{{alertname="{alertname}"}}'

    resp = await _get_http().get(f"{cfg.prometheus_url}/api/v1/alerts", params=params)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Elasticsearch tools
# ---------------------------------------------------------------------------


async def search_logs(
    cluster: str,
    query: str,
    from_time: str,
    to_time: str,
    size: int = 20,
) -> dict:
    """
    Search application logs in Elasticsearch for the given cluster.

    Args:
        cluster: Cluster name.
        query: Lucene/KQL query string (e.g. 'kubernetes.namespace:production AND level:ERROR').
        from_time: Start time in ISO 8601 or relative (e.g. "now-1h", "2024-01-01T00:00:00Z").
        to_time: End time in ISO 8601 or relative (e.g. "now").
        size: Maximum number of log entries to return (default: 20).

    Returns:
        Elasticsearch search response with hits.
    """
    cfg = _resolve_cluster(cluster)
    if not cfg.elasticsearch_url:
        return {"error": f"Elasticsearch not configured for cluster {cluster!r}"}

    index = cfg.elasticsearch_index_pattern
    body = {
        "size": size,
        "sort": [{"@timestamp": {"order": "desc"}}],
        "query": {
            "bool": {
                "must": [{"query_string": {"query": query}}],
                "filter": [{"range": {"@timestamp": {"gte": from_time, "lte": to_time}}}],
            }
        },
    }

    resp = await _get_http().post(
        f"{cfg.elasticsearch_url}/{index}/_search",
        json=body,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


async def get_index_stats(cluster: str, index: str | None = None) -> dict:
    """
    Get Elasticsearch index statistics for the given cluster.

    Args:
        cluster: Cluster name.
        index: Index name or pattern (defaults to cluster's configured pattern).

    Returns:
        Elasticsearch _stats response.
    """
    cfg = _resolve_cluster(cluster)
    if not cfg.elasticsearch_url:
        return {"error": f"Elasticsearch not configured for cluster {cluster!r}"}

    idx = index or cfg.elasticsearch_index_pattern
    resp = await _get_http().get(f"{cfg.elasticsearch_url}/{idx}/_stats")
    resp.raise_for_status()
    return resp.json()


async def close_http() -> None:
    global _http
    if _http:
        await _http.aclose()
        _http = None
