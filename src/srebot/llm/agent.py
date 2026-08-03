"""LLM agent proxy — delegates analysis to the SaaS Control Plane via WebSocket."""

import logging
from collections.abc import Awaitable, Callable

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
        self._response_language = settings.llm_response_language

    async def analyze(self, alerts: list[Alert]) -> tuple[str, str | None]:
        """
        Send a group of related alerts to the SaaS Backend for analysis.
        """
        if not self._token:
            return "⚠️ Cannot analyze: SAAS_AGENT_TOKEN is not configured.", None

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

        registry = get_mcp_registry()
        allowed_servers = registry.allowed_server_names(primary)
        tools_schema = get_tools_schema(allowed_servers=allowed_servers)

        client = SaaSWSClient(ws_url=self._ws_url, token=self._token)

        # Run the WebSocket loop
        return await client.analyze_alert(
            alert_data={"alerts": alert_data},
            tools_schema=tools_schema,
            tool_executor=call_tool,
            response_language=self._response_language,
        )

    async def followup(
        self,
        question: str,
        rca_text: str,
        alert_data: list[dict],
        allowed_servers: list[str] | None = None,
        incident_scoped: bool = True,
        parent_incident_id: str | None = None,
        user_name: str | None = None,
        on_tool_failure: Callable[[list[str]], Awaitable[None]] | None = None,
    ) -> tuple[str, str | None]:
        """
        Send a follow-up question to the SaaS backend with previous RCA as context.

        Args:
            question: The engineer's follow-up question.
            rca_text: Previous RCA analysis text shown to the engineer.
            alert_data: Original alert dicts for tool routing.
            allowed_servers: Optional additional MCP server restriction. Incident
                follow-ups are always re-scoped from alert_data. General queries use
                all registered servers only when this value is None.
            incident_scoped: Whether this request belongs to an incident. General
                queries must set this to False explicitly.
            parent_incident_id: ID of the parent incident.
            user_name: Username or display name of the user asking the question.
            on_tool_failure: Optional callback invoked with failed MCP tool names.

        Returns:
            Tuple of (answer, new_incident_id).
        """
        if not self._token:
            return "⚠️ Cannot answer: SAAS_AGENT_TOKEN is not configured.", None

        scoped_servers = self._followup_server_scope(
            alert_data,
            allowed_servers,
            incident_scoped=incident_scoped,
        )
        tools_schema = get_tools_schema(allowed_servers=scoped_servers)
        client = SaaSWSClient(ws_url=self._ws_url, token=self._token)

        return await client.analyze_followup(
            question=question,
            rca_context=rca_text,
            alert_data={"alerts": alert_data},
            tools_schema=tools_schema,
            parent_incident_id=parent_incident_id,
            response_language=self._response_language,
            user_name=user_name,
            on_tool_failure=on_tool_failure,
        )

    @staticmethod
    def _followup_server_scope(
        alert_data: list[dict],
        allowed_servers: list[str] | None,
        *,
        incident_scoped: bool,
    ) -> list[str] | None:
        """Resolve a safe MCP server scope for one follow-up request.

        Args:
            alert_data: Saved incident alerts, or an empty list for a general query.
            allowed_servers: Optional caller restriction applied after incident routing.
            incident_scoped: Whether fail-closed incident routing is required.

        Returns:
            Concrete incident server names, an empty fail-closed list for invalid
            incident context, or the caller value for an unscoped general query.
        """
        if not incident_scoped:
            return allowed_servers

        if not alert_data:
            logger.warning("Incident follow-up has no primary alert; disabling MCP tools")
            return []

        try:
            primary = Alert.model_validate(alert_data[0])
        except TypeError, ValueError:
            logger.warning("Incident follow-up has invalid primary alert; disabling MCP tools")
            return []

        derived_servers = get_mcp_registry().allowed_server_names(primary)
        if allowed_servers is None:
            return derived_servers

        caller_allowlist = set(allowed_servers)
        return [server for server in derived_servers if server in caller_allowlist]

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
            except (AttributeError, TypeError, ValueError) as exc:
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
