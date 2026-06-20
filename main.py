"""
Single entry point — runs the FastAPI web server and the Telegram bot
polling loop concurrently inside one async event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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

# ── Bot startup retry tuning ─────────────────────────────────────────
# When TELEGRAM_API_URL points at a Local Bot API server (see
# Dockerfile/supervisord.conf), that server and this process are
# started together by supervisord with no readiness check between
# them. The local server can take a few seconds to come up, so the
# bot's very first connection attempt can lose that race. Retrying
# with backoff makes that a non-issue instead of a permanent failure.
_BOT_STARTUP_MAX_RETRIES = int(os.getenv("BOT_STARTUP_MAX_RETRIES", "6"))
_BOT_STARTUP_RETRY_BASE_DELAY = float(os.getenv("BOT_STARTUP_RETRY_DELAY", "2.0"))
_BOT_STARTUP_RETRY_MAX_DELAY = 30.0


async def _run_uvicorn(shutdown_event: asyncio.Event) -> None:
    """Start the Uvicorn ASGI server in-process.

    We disable uvicorn's own signal handlers because main() installs a
    single set of handlers for the whole process (so the bot and the
    web server shut down together). That means *we* are responsible
    for telling uvicorn when to stop — a background watcher waits on
    shutdown_event and flips server.should_exit, which is the flag
    uvicorn's serve() loop actually checks.
    """
    uvi_config = uvicorn.Config(
        app=fastapi_app,
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        access_log=True,
    )
    server = uvicorn.Server(uvi_config)
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    async def _watch_for_shutdown() -> None:
        await shutdown_event.wait()
        server.should_exit = True

    watcher = asyncio.create_task(_watch_for_shutdown())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

    shutdown_event.set()


async def _run_bot(shutdown_event: asyncio.Event) -> None:
    """Start Telegram bot polling until shutdown is requested.

    Retries startup with backoff before giving up — this tolerates the
    Local Bot API server cold-start race described above, plus
    transient network blips. If every attempt fails, the error is
    logged and the web server keeps running regardless (a broken bot
    token shouldn't take down working video streams).
    """
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — Telegram bot will NOT start.")
        return

    application = None
    delay = _BOT_STARTUP_RETRY_BASE_DELAY

    for attempt in range(1, _BOT_STARTUP_MAX_RETRIES + 1):
        if shutdown_event.is_set():
            return

        application = build_application()
        try:
            await application.initialize()
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
            break  # success
        except Exception as exc:
            logger.warning(
                "Bot startup attempt %d/%d failed: %s",
                attempt, _BOT_STARTUP_MAX_RETRIES, exc,
            )
            with contextlib.suppress(Exception):
                await application.shutdown()
            application = None

            if attempt == _BOT_STARTUP_MAX_RETRIES:
                break

            # Wait before retrying, but wake up immediately if the
            # process is asked to shut down mid-backoff.
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, _BOT_STARTUP_RETRY_MAX_DELAY)

    if application is None:
        logger.error(
            "❌ Bot failed to start after %d attempts — giving up for this run. "
            "The web server keeps running; video streaming is unaffected. "
            "Common causes: the Local Bot API server (%s) wasn't reachable yet, "
            "or BOT_TOKEN is wrong. Check the logs above and restart if needed.",
            _BOT_STARTUP_MAX_RETRIES, config.TELEGRAM_API_URL,
        )
        return

    logger.info("🤖 Telegram bot is polling …")

    # Wait until the shutdown event is triggered.
    await shutdown_event.wait()

    logger.info("Stopping Telegram bot …")
    try:
        await application.updater.stop()  # type: ignore[union-attr]
        await application.stop()
        await application.shutdown()
    except Exception as exc:
        logger.warning("Error during bot shutdown: %s", exc)


def _task_exception(task: asyncio.Task) -> BaseException | None:
    """Safely pull an exception out of a finished task, if any."""
    if task.cancelled():
        return None
    return task.exception()


async def main() -> None:
    """Orchestrate the two long-running services."""
    shutdown_event = asyncio.Event()

    # Wire SIGINT / SIGTERM to set the event.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            # Not available on some platforms (e.g. Windows). Ctrl+C
            # still raises KeyboardInterrupt, handled below.
            pass

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

    bot_task = asyncio.create_task(_run_bot(shutdown_event), name="bot")
    web_task = asyncio.create_task(_run_uvicorn(shutdown_event), name="web")

    # Wait for either service to exit.
    done, pending = await asyncio.wait(
        {bot_task, web_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    fatal = False
    for task in done:
        exc = _task_exception(task)
        if exc is not None:
            logger.error("%s task crashed:", task.get_name(), exc_info=exc)
            fatal = True

    # If only the bot exited (failed/stopped) and nothing crashed, keep
    # the web server alive — that's the whole point of the retry logic
    # above plus this fallback.
    if bot_task in done and web_task in pending and not fatal:
        logger.warning("Bot stopped early. Web server continues running.")
        await web_task
        web_exc = _task_exception(web_task)
        if web_exc is not None:
            logger.error("web task crashed:", exc_info=web_exc)
            fatal = True
    else:
        # Web server exited (or both did) — shut everything down.
        shutdown_event.set()
        for task in pending:
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            except Exception as exc:
                logger.error("%s task raised during shutdown:", task.get_name(), exc_info=exc)
                fatal = True

    if fatal:
        logger.error("❌ Shutting down due to a fatal error — see above.")
        sys.exit(1)

    logger.info("✅ Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
