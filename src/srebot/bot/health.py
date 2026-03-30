"""
Standalone HTTP server for Kubernetes liveness and readiness probes.

Runs independently of any bot integration so that K8s probes always work
regardless of which platform is in use.
"""

import logging

from aiohttp import web

from srebot.state.store import get_store

logger = logging.getLogger(__name__)

_HEALTH_PORT = 8080


async def _liveness_handler(request: web.Request) -> web.Response:
    """Always returns 200 OK — indicates the event loop is running."""
    return web.Response(text="OK", status=200)


async def _readiness_handler(request: web.Request) -> web.Response:
    """Check critical dependencies, primarily Redis."""
    try:
        store = await get_store()
        await store.ping()
        return web.Response(text="Ready", status=200)
    except Exception as e:
        logger.error("Readiness check failed: %s", e)
        return web.Response(text=f"Not Ready: {e}", status=503)


async def start_health_server() -> web.AppRunner:
    """
    Start the aiohttp healthcheck server on port 8080.

    Returns:
        A running ``web.AppRunner`` instance. Call ``runner.cleanup()``
        to shut it down gracefully.
    """
    app = web.Application()
    app.router.add_get("/livez", _liveness_handler)
    app.router.add_get("/readyz", _readiness_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", _HEALTH_PORT)
    await site.start()

    logger.info("Healthcheck HTTP server started on port %d (/livez, /readyz)", _HEALTH_PORT)
    return runner
