"""Tests for bot/shared.py — common alert-processing pipeline."""

from unittest.mock import AsyncMock, MagicMock, patch

from srebot.bot.shared import group_key, process_alert_text
from srebot.parser.alert_parser import Alert, update_remote_strategies


def setup_module(module):
    """Seed the dynamic parser with same strategies used in prod for local tests."""
    update_remote_strategies(
        [
            {
                "name": "Standard",
                "firing_pattern": r"(alerts?\s+firing|\[FIRING:.*\]|FIRING|🔥|\*Alert:\*)",
                "resolved_pattern": r"(alerts?\s+resolved|\[RESOLVED:.*\]|RESOLVED|✅|\[RESOLVED\])",  # noqa: E501
                "labels_header_pattern": r"^(Labels|Details):\s*$",
                "annotations_header_pattern": r"^Annotations:\s*$",
                "kv_pattern": r"^\s*[\-•]\s*(.+?)\s*[=:]\s*(.+)$",
                "priority": 10,
            },
            {
                "name": "Markdown",
                "firing_pattern": r"(alerts?\s+firing|\[FIRING:.*\]|FIRING|🔥|\*Alert:\*)",
                "resolved_pattern": r"(alerts?\s+resolved|\[RESOLVED:.*\]|RESOLVED|✅|\[RESOLVED\])",  # noqa: E501
                "labels_header_pattern": r"^\*?(Details|Labels):\*?\s*$",
                "annotations_header_pattern": r"^\*?Annotations:\*?\s*$",
                "kv_pattern": r"^\s*\*?\s*[•\-]?\s*\*?\s*(.+?)\s*\*?\s*[:=]\*?\s*(.+)$",
                "priority": 20,
            },
        ]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIRING_TEXT = (
    "Alerts Firing:\n"
    "Labels:\n"
    " - alertname = CPUHigh\n"
    " - cluster = prod\n"
    " - job = api-server\n"
    " - severity = critical\n"
    " - namespace = default\n"
    "Annotations:\n"
    " - summary = CPU is too high\n"
    "Source: https://prometheus.example.com/graph\n"
)

FIRING_TEXT_TWO_GROUPS = (
    "Alerts Firing:\n"
    "Labels:\n"
    " - alertname = CPUHigh\n"
    " - cluster = prod\n"
    " - job = api-server\n"
    " - severity = critical\n"
    " - namespace = default\n"
    "Annotations:\n"
    " - summary = CPU high\n"
    "Source: https://prometheus.example.com/graph\n"
    "Labels:\n"
    " - alertname = MemoryHigh\n"
    " - cluster = prod\n"
    " - job = api-server\n"
    " - severity = warning\n"
    " - namespace = default\n"
    "Annotations:\n"
    " - summary = Memory high\n"
    "Source: https://prometheus.example.com/graph\n"
)


def _make_alert(alertname="CPUHigh", cluster="prod", job="api") -> Alert:
    return Alert(
        status="firing",
        alertname=alertname,
        cluster=cluster,
        labels={"job": job},
        annotations={},
        fingerprint="abc123",
        namespace="default",
        severity="critical",
        source_url="",
    )


# ---------------------------------------------------------------------------
# group_key
# ---------------------------------------------------------------------------


class TestGroupKey:
    def test_same_inputs_produce_same_key(self):
        a = _make_alert("CPUHigh", "prod", "api")
        assert group_key(a) == group_key(a)

    def test_different_alertname_produces_different_key(self):
        a = _make_alert("CPUHigh", "prod", "api")
        b = _make_alert("MemHigh", "prod", "api")
        assert group_key(a) != group_key(b)

    def test_different_cluster_produces_different_key(self):
        a = _make_alert("CPUHigh", "prod", "api")
        b = _make_alert("CPUHigh", "staging", "api")
        assert group_key(a) != group_key(b)

    def test_different_job_produces_different_key(self):
        a = _make_alert("CPUHigh", "prod", "api")
        b = _make_alert("CPUHigh", "prod", "worker")
        assert group_key(a) != group_key(b)

    def test_key_is_16_chars(self):
        a = _make_alert()
        assert len(group_key(a)) == 16

    def test_missing_job_label_uses_empty_string(self):
        a = Alert(
            status="firing",
            alertname="Test",
            cluster="prod",
            labels={},  # no job
            annotations={},
            fingerprint="x",
            namespace="ns",
            severity="info",
            source_url="",
        )
        key = group_key(a)
        assert len(key) == 16


# ---------------------------------------------------------------------------
# process_alert_text
# ---------------------------------------------------------------------------


class TestProcessAlertText:
    async def test_non_alert_text_skips_handler(self):
        handler = AsyncMock()
        agent = MagicMock()
        agent.parse_raw_text = AsyncMock(return_value=[])
        with patch("srebot.llm.agent.get_agent", return_value=agent):
            await process_alert_text("Hello, World!", handler)
        handler.assert_not_called()

    async def test_empty_text_skips_handler(self):
        handler = AsyncMock()
        agent = MagicMock()
        agent.parse_raw_text = AsyncMock(return_value=[])
        with patch("srebot.llm.agent.get_agent", return_value=agent):
            await process_alert_text("", handler)
        handler.assert_not_called()

    async def test_single_alert_calls_handler_once(self):
        handler = AsyncMock()
        await process_alert_text(FIRING_TEXT, handler)
        handler.assert_called_once()

    async def test_handler_receives_fingerprint_and_alerts(self):
        handler = AsyncMock()
        await process_alert_text(FIRING_TEXT, handler)
        fp, alerts = handler.call_args[0][:2]
        assert isinstance(fp, str) and len(fp) == 16
        assert isinstance(alerts, list) and len(alerts) == 1

    async def test_extra_args_forwarded_to_handler(self):
        handler = AsyncMock()
        mock_store = AsyncMock()
        mock_store.is_muted.return_value = False

        with patch("srebot.state.store.get_store", return_value=mock_store):
            await process_alert_text(FIRING_TEXT, handler, "chan-123", "client-obj")

        _fp, _alerts, chan, client = handler.call_args[0]
        assert chan == "chan-123"
        assert client == "client-obj"
        mock_store.is_muted.assert_called_once_with("slack:chan-123", "CPUHigh")

    async def test_two_groups_calls_handler_twice(self):
        handler = AsyncMock()
        await process_alert_text(FIRING_TEXT_TWO_GROUPS, handler)
        assert handler.call_count == 2

    async def test_ignored_alerts_are_filtered_out(self):
        handler = AsyncMock()
        ignore_registry = MagicMock()
        ignore_registry.should_ignore.return_value = True  # ignore everything
        with patch("srebot.bot.shared.get_ignore_registry", return_value=ignore_registry):
            await process_alert_text(FIRING_TEXT, handler)
        handler.assert_not_called()

    async def test_handler_exception_does_not_propagate(self):
        """Errors inside the handler are caught by gather — process_alert_text must not raise."""
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        # Should not raise
        await process_alert_text(FIRING_TEXT, handler)

    async def test_muted_alerts_are_filtered_out(self):
        handler = AsyncMock()
        mock_store = AsyncMock()
        mock_store.is_muted.return_value = True  # Mute everything

        # Mocking extract_chat_id to return "telegram:123"
        with (
            patch("srebot.bot.commands.extract_chat_id", return_value="telegram:123"),
            patch("srebot.state.store.get_store", return_value=mock_store),
        ):
            await process_alert_text(FIRING_TEXT, handler, "dummy_msg_obj_or_args")

        handler.assert_not_called()
        mock_store.is_muted.assert_called_once_with("telegram:123", "CPUHigh")
