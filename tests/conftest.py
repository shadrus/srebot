"""Shared pytest fixtures and configuration."""

from unittest.mock import patch

import pytest

from srebot.bot.concurrency import ConcurrencyManager

# Make all tests async-capable with pytest-asyncio
pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _analysis_concurrency():
    """Give every test an isolated real concurrency manager.

    Tests that mock ``config.get_settings`` would otherwise construct the
    shared manager from MagicMock attributes. Dedicated concurrency tests
    patch ``get_concurrency_manager`` again with tighter limits.
    """
    manager = ConcurrencyManager(max_total=20, max_per_user=3)
    with patch("srebot.bot.shared.get_concurrency_manager", return_value=manager):
        yield manager
