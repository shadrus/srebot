"""LLM agent — runs the tool-call loop to analyze a Prometheus alert."""

import logging

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage

from ai_observability_bot.config import get_mcp_registry, get_settings
from ai_observability_bot.llm.prompts import SYSTEM_PROMPT, build_user_message
from ai_observability_bot.mcp.registry import call_tool, get_tools_schema
from ai_observability_bot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)


class AlertAnalysisAgent:
    """
    Runs an agentic LLM tool-call loop to analyze a Prometheus alert.

    Uses any OpenAI-compatible API (OpenAI, Azure OpenAI, vLLM, LM Studio…)
    configured via LLM_BASE_URL + LLM_API_KEY.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
        self._model = settings.llm_model
        self._language = settings.llm_response_language
        self._max_iterations = settings.llm_max_iterations

    async def analyze(self, alerts: list[Alert]) -> str:
        """
        Analyze a group of related alerts and return a Telegram HTML-formatted string.
        All alerts share the same alertname + cluster + job.
        Runs the LLM tool-calling loop until the model produces a final text response.
        """
        settings = get_settings()
        primary = alerts[0]
        messages: list[dict] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(
                    language=self._language, bot_name=settings.bot_container_name
                ),
            },
            {"role": "user", "content": build_user_message(alerts)},
        ]
        used_tools: set[str] = set()

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

        for iteration in range(self._max_iterations):
            logger.debug(
                "LLM iteration %d/%d for group %s (%d alert(s))",
                iteration + 1,
                self._max_iterations,
                primary.alertname,
                len(alerts),
            )

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools_schema if tools_schema else None,
                tool_choice="auto" if tools_schema else "none",
            )

            message: ChatCompletionMessage = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            # Append assistant message (may include tool_calls)
            messages.append(message.model_dump(exclude_none=True))

            if finish_reason == "stop" or not message.tool_calls:
                # Final answer
                content = message.content or "⚠️ Analysis returned empty response."
                if used_tools:
                    tool_names = {sig.split(":", 1)[0] for sig in used_tools}
                    tools_str = ", ".join(f"<code>{t}</code>" for t in sorted(tool_names))
                    content += f"\n\n<b>🛠 Tools used ({iteration + 1}):</b> {tools_str}"
                return content

            # Execute all requested tool calls
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = tool_call.function.arguments
                
                # Check for duplicate calls with exact same arguments
                call_signature = f"{fn_name}:{fn_args}"
                
                if call_signature in used_tools:
                    logger.warning("Prevented duplicate tool call: %s", fn_name)
                    result = (
                        f"⚠️ Error: You already called '{fn_name}' with exactly these arguments. "
                        "The result is already in the conversation history above. "
                        "Do NOT repeat this exact call. Try a different query or provide your final analysis."
                    )
                else:
                    used_tools.add(call_signature)
                    logger.info("Tool call: %s(%s)", fn_name, fn_args[:200])

                    result = await call_tool(fn_name, fn_args)
                    logger.debug("Tool result for %s: %s", fn_name, result[:500])

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

        logger.warning(
            "Max iterations (%d) reached for group %s", self._max_iterations, primary.alertname
        )
        return "⚠️ Analysis incomplete: max tool iterations reached. Check logs for details."


# Module-level singleton
_agent: AlertAnalysisAgent | None = None


def get_agent() -> AlertAnalysisAgent:
    global _agent
    if _agent is None:
        _agent = AlertAnalysisAgent()
    return _agent
