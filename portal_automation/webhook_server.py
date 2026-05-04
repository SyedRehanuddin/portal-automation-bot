from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from .config import load_config
from .telegram_bot import build_application


LOGGER = logging.getLogger(__name__)


def _config_path() -> str:
    return os.getenv("CONFIG_PATH", "config.json")


def _webhook_path() -> str:
    path = os.getenv("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path


def _webhook_url(path: str) -> str:
    configured_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if configured_url:
        return configured_url.rstrip("/")

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return f"{render_url}{path}"

    raise RuntimeError("Set TELEGRAM_WEBHOOK_URL or RENDER_EXTERNAL_URL for webhook deployment.")


def create_app() -> FastAPI:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    config = load_config(_config_path())
    telegram_app = build_application(config)
    webhook_path = _webhook_path()
    secret_token = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await telegram_app.initialize()
        await telegram_app.start()

        if os.getenv("SKIP_SET_WEBHOOK", "").strip().lower() not in {"1", "true", "yes"}:
            url = _webhook_url(webhook_path)
            LOGGER.info("Setting Telegram webhook to %s", url)
            await telegram_app.bot.set_webhook(
                url=url,
                allowed_updates=Update.ALL_TYPES,
                secret_token=secret_token or None,
                drop_pending_updates=False,
            )

        try:
            yield
        finally:
            LOGGER.info("Stopping Telegram webhook application.")
            await telegram_app.stop()
            await telegram_app.shutdown()

    app = FastAPI(title="Portal Automation Telegram Bot", lifespan=lifespan)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"status": "ok", "service": "portal_automation"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(webhook_path)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if secret_token and x_telegram_bot_api_secret_token != secret_token:
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret.")

        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}

    return app


app = create_app()

