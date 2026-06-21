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

    # We manage shutdown ourselves via shutdown_event, so disable
    # uvicorn's own signal handlers...
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    # ...but that means something has to translate shutdown_event into
    # uvicorn's own stop condition, or server.serve() below never
    # returns on SIGINT/SIGTERM and the process hangs until killed.
    async def _watch_shutdown() -> None:
        await shutdown_event.wait()
        server.should_exit = True

    watcher = asyncio.create_task(_watch_shutdown())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        # Also true if uvicorn exited on its own (crash, lifespan
        # failure) — make sure the bot task gets told to stop too.
        shutdown_event.set()


async def _run_bot(shutdown_event: asyncio.Event) -> None:
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — Telegram bot will NOT start.")
        return
    application = build_application()
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
    except Exception as exc:
        logger.error("❌ Bot failed to start: %s", exc)
        try:
            await application.shutdown()
        except Exception:
            pass
        return
    logger.info("🤖 Telegram bot is polling …")
    await shutdown_event.wait()
    logger.info("Stopping Telegram bot …")
    try:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    except Exception as exc:
        logger.warning("Error during bot shutdown: %s", exc)


async def main() -> None:
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    config.validate()
    print(_BANNER)
    logger.info("🚀 Starting Telegram Stream Proxy v3.0")
    logger.info("   ├── Server:  http://%s:%d", config.HOST, config.PORT)
    logger.info("   ├── Public:  %s", config.BASE_URL)
    logger.info("   ├── HLS:     %s", "enabled" if config.ENABLE_HLS else "disabled")
    logger.info("   ├── Remux:   %s", "enabled" if config.ENABLE_REMUX else "disabled")
    logger.info("   └── Log level: %s", config.LOG_LEVEL)

    bot_task = asyncio.create_task(_run_bot(shutdown_event), name="bot")
    web_task = asyncio.create_task(_run_uvicorn(shutdown_event), name="web")

    done, pending = await asyncio.wait({bot_task, web_task}, return_when=asyncio.FIRST_COMPLETED)

    if shutdown_event.is_set():
        # Deliberate shutdown (SIGINT/SIGTERM) — both tasks are already
        # winding down on their own; just wait for whichever is left.
        logger.info("Shutdown requested — waiting for services to stop…")
        for task in pending:
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("%s did not stop in time, cancelling.", task.get_name())
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    elif bot_task in done and web_task in pending:
        # Bot returned on its own without a shutdown being requested —
        # e.g. missing/invalid BOT_TOKEN. Keep serving the web app.
        logger.warning("Bot stopped early (not via shutdown signal). Web server continues running.")
        await web_task
    else:
        # Web server exited unexpectedly (crash, lifespan failure) —
        # bring the bot down too rather than leaving it polling alone.
        logger.error("Web server exited unexpectedly — shutting everything down.")
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
