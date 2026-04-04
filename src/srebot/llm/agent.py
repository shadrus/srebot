"""LLM agent proxy — delegates analysis to the SaaS Control Plane via WebSocket."""

import logging

from srebot.config import get_mcp_registry, get_settings
from srebot.llm.ws_client import SaaSWSClient
from srebot.mcp.registry import call_tool, get_tools_schema
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)


class AlertAnalysisAgent:
    """
    Proxies alert analysis to the SaaS Control Plane instead of running LLM locally.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._ws_url = settings.saas_ws_url
        self._token = settings.saas_agent_token

    async def analyze(self, alerts: list[Alert]) -> str:
        """
        Send a group of related alerts to the SaaS Backend for analysis.
        """
        if not self._token:
            return "⚠️ Cannot analyze: SAAS_AGENT_TOKEN is not configured."

        primary = alerts[0]

        # Serialize alerts to dict for JSON transport
        alert_data = [
            {
                "status": a.status,
                "alertname": a.alertname,
                "cluster": a.cluster,
                "namespace": a.namespace,
                "severity": a.severity,
                "labels": a.labels,
                "annotations": a.annotations,
                "fingerprint": a.fingerprint,
                "source_url": a.source_url,
            }
            for a in alerts
        ]

        # Determine which external MCP servers are allowed for this alert group
        registry = get_mcp_registry()
        allowed_servers: list[str] = []
        for server in registry.all_configs():
            if server.condition is None or server.condition.matches(primary):
                allowed_servers.append(server.name)
            else:
                logger.debug(
                    "Server %r blocked for group %s by condition", server.name, primary.alertname
                )

        # Get schema containing only tools from allowed servers
        tools_schema = get_tools_schema(allowed_servers=allowed_servers)

        client = SaaSWSClient(ws_url=self._ws_url, token=self._token)

        # Run the WebSocket loop
        return await client.analyze_alert(
            alert_data={"alerts": alert_data},
            tools_schema=tools_schema,
            tool_executor=call_tool,
        )

    async def parse_raw_text(self, text: str) -> list[Alert]:
        """
        Identify and extract Alert objects from unstructured text via SaaS LLM.
        """
        if not self._token:
            logger.warning("Cannot smart-parse: SAAS_AGENT_TOKEN is not configured.")
            return []

        client = SaaSWSClient(ws_url=self._ws_url, token=self._token)
        raw_alerts = await client.extract_alerts(text)

        alerts: list[Alert] = []
        for a in raw_alerts:
            try:
                # Ensure all required fields are present; others get defaults
                alerts.append(
                    Alert(
                        status=a.get("status", "firing"),
                        alertname=a.get("alertname", "unknown"),
                        cluster=a.get("cluster", "unknown"),
                        namespace=a.get("namespace", ""),
                        severity=a.get("severity", ""),
                        labels=a.get("labels", {}),
                        annotations=a.get("annotations", {}),
                        fingerprint=a.get("fingerprint", ""),
                        source_url=a.get("source_url"),
                    )
                )
            except Exception as exc:
                logger.warning("Failed to construct Alert from SaaS data: %s | data=%s", exc, a)

        return alerts

    async def refresh_strategies(self) -> None:
        """Fetch latest dynamic parsing strategies from the SaaS backend."""
        if not self._token:
            logger.debug("Skipping strategy refresh: SAAS_AGENT_TOKEN not set.")
            return

        client = SaaSWSClient(ws_url=self._ws_url, token=self._token)
        await client.refresh_strategies()


# Module-level singleton
_agent: AlertAnalysisAgent | None = None


def get_agent() -> AlertAnalysisAgent:
    global _agent
    if _agent is None:
        _agent = AlertAnalysisAgent()
    return _agent
