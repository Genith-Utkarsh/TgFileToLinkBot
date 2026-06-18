"""
Single entry point — runs the FastAPI web server and the Telegram bot
polling loop concurrently inside one async event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

import config
from bot import build_application
from server import app as fastapi_app

# ── Startup banner ──────────────────────────────────────────────────
_BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   ████████╗ ██████╗     ███████╗████████╗██████╗         ║
║   ╚══██╔══╝██╔════╝     ██╔════╝╚══██╔══╝██╔══██╗       ║
║      ██║   ██║  ███╗    ███████╗   ██║   ██████╔╝       ║
║      ██║   ██║   ██║    ╚════██║   ██║   ██╔══██╗       ║
║      ██║   ╚██████╔╝    ███████║   ██║   ██║  ██║       ║
║      ╚═╝    ╚═════╝     ╚══════╝   ╚═╝   ╚═╝  ╚═╝       ║
║                                                          ║
║           Telegram Stream Proxy  v2.0                    ║
║           Zero-cache media streaming                     ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
"""

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


async def _run_uvicorn(shutdown_event: asyncio.Event) -> None:
    """Start the Uvicorn ASGI server in-process."""
    uvi_config = uvicorn.Config(
        app=fastapi_app,
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        access_log=True,
    )
    server = uvicorn.Server(uvi_config)

    # Override the default signal handlers so we can manage shutdown
    # ourselves from the main coroutine.
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    await server.serve()
    shutdown_event.set()


async def _run_bot(shutdown_event: asyncio.Event) -> None:
    """Start Telegram bot polling until shutdown is requested."""
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — Telegram bot will NOT start.")
        return

    application = build_application()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

    logger.info("🤖 Telegram bot is polling …")

    # Wait until the shutdown event is triggered.
    await shutdown_event.wait()

    logger.info("Stopping Telegram bot …")
    await application.updater.stop()  # type: ignore[union-attr]
    await application.stop()
    await application.shutdown()


async def main() -> None:
    """Orchestrate the two long-running services."""
    shutdown_event = asyncio.Event()

    # Wire SIGINT / SIGTERM to set the event.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    # Validate config before starting
    config.validate()

    # Print startup banner
    print(_BANNER)
    logger.info("🚀 Starting Telegram Stream Proxy v2.0")
    logger.info("   ├── Server:  http://%s:%d", config.HOST, config.PORT)
    logger.info("   ├── Public:  %s", config.BASE_URL)
    logger.info("   ├── Player:  %s/", config.BASE_URL)
    logger.info("   ├── Health:  %s/health", config.BASE_URL)
    logger.info("   └── Log level: %s", config.LOG_LEVEL)

    bot_task = asyncio.create_task(_run_bot(shutdown_event))
    web_task = asyncio.create_task(_run_uvicorn(shutdown_event))

    # Wait for either service to exit (normally the web server shutting
    # down triggers bot shutdown via the shared event).
    done, pending = await asyncio.wait(
        {bot_task, web_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Ensure the other task finishes cleanly.
    shutdown_event.set()
    for task in pending:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    logger.info("✅ Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
