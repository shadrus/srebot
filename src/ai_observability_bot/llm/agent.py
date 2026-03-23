"""LLM agent proxy — delegates analysis to the SaaS Control Plane via WebSocket."""

import logging

from ai_observability_bot.config import get_mcp_registry, get_settings
from ai_observability_bot.llm.ws_client import SaaSWSClient
from ai_observability_bot.mcp.registry import call_tool, get_tools_schema
from ai_observability_bot.parser.alert_parser import Alert

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


# Module-level singleton
_agent: AlertAnalysisAgent | None = None


def get_agent() -> AlertAnalysisAgent:
    global _agent
    if _agent is None:
        _agent = AlertAnalysisAgent()
    return _agent
