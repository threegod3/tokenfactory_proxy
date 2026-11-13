from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import AppConfig, load_config
from .errors import ProxyError
from .logging_setup import setup_logging
from .routes import admin as admin_routes
from .routes import anthropic as anthropic_routes
from .routes import openai as openai_routes
from .services.account_pool import AccountPool
from .services.chat import ChatService
from .services.models import ModelRegistry
from .upstream.client import TokenFactoryClient
from .upstream.http import UpstreamSession

logger = logging.getLogger(__name__)


class AppState:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.session = UpstreamSession(cfg)
        self.registry = ModelRegistry(cfg)
        self.pool: Optional[AccountPool] = AccountPool(cfg)
        from .services.quota import QuotaTracker
        self.quota = QuotaTracker(cfg)
        self.chat = ChatService(self.session, self.registry, self.pool, self.quota)
        self.client = TokenFactoryClient(self.session)
        self.started_at = time.time()


async def _refresh_models(state: AppState) -> None:
    try:
        payload = await state.client.get_public_models()
        state.registry.upsert_from_upstream(payload)
    except Exception as e:
        state.registry.last_error = str(e)
        logger.warning("model catalog refresh failed: %s", e)


def create_app(cfg: Optional[AppConfig] = None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg.server.log_level, cfg.base_dir / "logs")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = AppState(cfg)
        app.state.tf = state
        await state.session.start()
        logger.info(
            "tfproxy ready: upstream=%s proxy=%s cookies=%d",
            cfg.upstream.base_url,
            cfg.upstream.proxy,
            len(state.session.cookies),
        )
        await _refresh_models(state)
        if cfg.models.refresh_interval_seconds and cfg.models.refresh_interval_seconds > 0:

            async def model_loop():
                while True:
                    await asyncio.sleep(cfg.models.refresh_interval_seconds)
                    await _refresh_models(state)

            task = asyncio.create_task(model_loop())
        else:
            task = None

        # 主动续期：/internal/auth/refresh 只在 access token 仍有效时可用，
        # 因此必须在过期前刷新。检测到剩余 < 300s 即刷新（token 寿命约 600s）。
        async def token_loop():
            await asyncio.sleep(5)
            while True:
                try:
                    st = state.session.session_state()
                    if not st.get("has_cookies"):
                        await asyncio.sleep(30)
                        continue
                    left = st.get("access_expires_in")
                    if left is not None and left > 300:
                        # 还很充裕，睡到进入续期窗口
                        await asyncio.sleep(min(120.0, max(20.0, left - 280)))
                        continue
                    if left is not None and left > 0:
                        ok = await state.session.refresh_access_token()
                        if ok:
                            logger.info("proactive refresh ok, next window in ~%ss",
                                        round((state.session.access_token_expires_at() or 0) - 300 - time.time()))
                            # refresh 会轮换 REFRESH_TOKEN，同步回账号池与 accounts.json
                            if state.pool is not None:
                                state.pool.sync_active_cookies(state.session)
                        else:
                            logger.warning("proactive refresh failed while token still valid")
                        await asyncio.sleep(30)
                        continue
                    # token 已过期：纯协议无法续期，只能用账号池做完整登录（需浏览器取 token）
                    logger.warning("access token already expired (%.0fs); full login required", left or 0)
                    if state.pool is not None and state.pool.accounts:
                        ok = await state.pool.ensure_session(state.session)
                        logger.info("full login via account pool: %s", "ok" if ok else "failed")
                    else:
                        logger.error("no accounts configured; cannot recover session automatically")
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("token loop error: %s", e)
                    await asyncio.sleep(30)

        token_task = asyncio.create_task(token_loop())

        # REFRESH_TOKEN 到期巡检 + 账号池热重载（注册进程持续产出新号）
        async def maintenance_loop():
            await asyncio.sleep(60)
            while True:
                try:
                    if state.pool is not None:
                        total = state.pool.reload_accounts()
                        if summary := await state.pool.maintenance(state.session):
                            logger.info("maintenance cycle result: %s", summary)
                        if total and total % 10 == 0:
                            logger.info("account pool size: %d", total)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("maintenance loop error: %s", e)
                await asyncio.sleep(max(60, min(300, cfg.maintenance.interval_seconds // 6)))

        maint_task = asyncio.create_task(maintenance_loop())
        try:
            yield
        finally:
            for t in (task, token_task, maint_task):
                if t:
                    t.cancel()
            await state.session.aclose()
            logger.info("tfproxy shutdown")

    app = FastAPI(title="TokenFactory Reverse Proxy", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(ProxyError)
    async def proxy_error_handler(request: Request, exc: ProxyError):
        logger.warning("proxy error %s: %s", exc.status_code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_openai_error())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Request validation failed",
                    "type": "invalid_request_error",
                    "param": None,
                    "details": exc.errors(),
                }
            },
        )

    from fastapi.exceptions import HTTPException as FastAPIHTTPException

    @app.exception_handler(FastAPIHTTPException)
    async def http_error_handler(request: Request, exc: FastAPIHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"message": str(detail), "type": "proxy_error"}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def generic_handler(request: Request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"internal proxy error: {exc}", "type": "proxy_error"}},
        )

    app.include_router(openai_routes.router)
    app.include_router(anthropic_routes.router)
    if cfg.admin.enabled:
        app.include_router(admin_routes.router)
    return app


app: Optional[FastAPI] = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app(load_config())
    return app
