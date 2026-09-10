"""Proxy environment resolution without network access."""

import pytest

from srebot.bot.proxy import environment_proxy


@pytest.fixture
def proxy_environment(monkeypatch):
    for name in ("http", "https", "all", "no"):
        monkeypatch.delenv(f"{name}_proxy", raising=False)
        monkeypatch.delenv(f"{name.upper()}_PROXY", raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("variables", "destination", "expected"),
    [
        ({}, "https://discord.com/api/v10", None),
        ({"HTTPS_PROXY": ""}, "https://discord.com", None),
        (
            {"HTTPS_PROXY": "http://proxy.test:3128"},
            "https://discord.com/api/v10",
            "http://proxy.test:3128",
        ),
        (
            {"https_proxy": "http://proxy.test:3128"},
            "https://time.test/api/v4",
            "http://proxy.test:3128",
        ),
        (
            {"HTTPS_PROXY": "http://user:p%40ss@proxy.test:3128"},
            "https://time.test",
            "http://user:p%40ss@proxy.test:3128",
        ),
        (
            {"HTTPS_PROXY": "http://upper.test", "https_proxy": "http://lower.test"},
            "https://discord.com",
            "http://lower.test",
        ),
        (
            {"HTTPS_PROXY": "http://proxy.test", "NO_PROXY": ".time.test"},
            "https://internal.time.test/api/v4",
            None,
        ),
        (
            {"HTTPS_PROXY": "http://proxy.test", "NO_PROXY": "*"},
            "https://discord.com",
            None,
        ),
        (
            {"HTTPS_PROXY": "http://proxy.test", "NO_PROXY": "other.test"},
            "https://discord.com",
            "http://proxy.test",
        ),
        ({"HTTPS_PROXY": "http://proxy.test"}, "http://time.test", None),
        (
            {"HTTP_PROXY": "http://proxy.test"},
            "http://time.test",
            "http://proxy.test",
        ),
    ],
)
def test_environment_proxy(proxy_environment, variables, destination, expected):
    for key, value in variables.items():
        proxy_environment.setenv(key, value)
    assert environment_proxy(destination) == expected
