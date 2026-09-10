"""Environment proxy selection for chat SDKs without automatic support."""

from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass


def environment_proxy(url: str) -> str | None:
    """Resolve the destination's proxy using standard environment variables.

    Args:
        url: Absolute HTTP or HTTPS destination URL.

    Returns:
        Proxy URL, or None when unset or excluded by NO_PROXY.
    """
    destination = urlsplit(url)
    proxies = getproxies()
    if proxy_bypass(destination.netloc):
        return None
    return proxies.get(destination.scheme) or proxies.get("all")
