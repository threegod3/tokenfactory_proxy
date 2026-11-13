from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> Dict[str, Any]:
    state = request.app.state.tf
    sess = state.session.session_state()
    return {
        "status": "ok",
        "service": "tfproxy",
        "uptime_seconds": round(time.time() - state.started_at, 1),
        "upstream": state.session.cfg.upstream.base_url,
        "has_session_cookies": bool(sess.get("has_cookies")),
        "session": sess,
        "tenant_id": state.session.tenant_id or None,
        "workspace_id": state.session.workspace_id or None,
        "models_cached": len(state.registry.models),
        "models_source": state.registry.source,
        "accounts": len(state.pool.accounts) if state.pool else 0,
    }


def _admin_ok(request: Request) -> bool:
    state = request.app.state.tf
    token = state.cfg.admin.token
    if not token:
        return request.headers.get("authorization", "").startswith("Bearer ") or True
    auth = request.headers.get("authorization", "")
    return auth == f"Bearer {token}"


@router.get("/admin/status")
async def admin_status(request: Request) -> Dict[str, Any]:
    state = request.app.state.tf
    if not _admin_ok(request):
        return {"ok": False, "error": "unauthorized"}
    return {
        "ok": True,
        "config": {
            "base_url": state.cfg.upstream.base_url,
            "proxy": state.cfg.upstream.proxy,
            "chat_mode": state.cfg.upstream.chat_mode,
            "reuse_conversation": state.cfg.upstream.reuse_conversation,
            "recaptcha_mode": state.cfg.auth.recaptcha_mode,
            "daily_quota_per_account": state.cfg.upstream.daily_quota_per_account,
        },
        "session": {
            "cookies": sorted(state.session.cookies.keys()),
            "tenant_id": state.session.tenant_id,
            "workspace_id": state.session.workspace_id,
            "email": getattr(state.session, "email", None),
            "loaded_at": state.session.session_loaded_at,
        },
        "quota": state.quota.status() if hasattr(state, "quota") else {},
        "models": {
            "count": len(state.registry.models),
            "source": state.registry.source,
            "last_refresh": state.registry.last_refresh,
            "last_error": state.registry.last_error,
            "aliases": state.registry.aliases,
        },
        "accounts": state.pool.status() if state.pool else [],
    }


@router.post("/admin/reload-session")
async def admin_reload_session(request: Request) -> Dict[str, Any]:
    state = request.app.state.tf
    if not _admin_ok(request):
        return {"ok": False, "error": "unauthorized"}
    await state.session.load_cookies()
    return {"ok": True, "cookies": sorted(state.session.cookies.keys()),
            "session": state.session.session_state()}


@router.post("/admin/refresh-token")
async def admin_refresh_token(request: Request) -> Dict[str, Any]:
    """纯协议续期：用 REFRESH_TOKEN 换新的 ACCESS_TOKEN（不启动浏览器）。"""
    state = request.app.state.tf
    if not _admin_ok(request):
        return {"ok": False, "error": "unauthorized"}
    before = state.session.session_state()
    ok = await state.session.refresh_access_token()
    if ok and state.pool is not None:
        state.pool.sync_active_cookies(state.session)
    return {"ok": ok, "before": before, "after": state.session.session_state()}


@router.post("/admin/reload-accounts")
async def admin_reload_accounts(request: Request) -> Dict[str, Any]:
    """热重载账号池（注册进程产出的新号立即可用）。"""
    state = request.app.state.tf
    if not _admin_ok(request):
        return {"ok": False, "error": "unauthorized"}
    total = state.pool.reload_accounts()
    return {"ok": True, "pool_total": total}


@router.post("/admin/maintenance")
async def admin_maintenance(request: Request) -> Dict[str, Any]:
    """手动触发一次 REFRESH_TOKEN 到期巡检（临期账号起浏览器续期，并发受限）。"""
    state = request.app.state.tf
    if not _admin_ok(request):
        return {"ok": False, "error": "unauthorized"}
    summary = await state.pool.maintenance(state.session)
    return {"ok": True, "summary": summary, "accounts": state.pool.status()}


@router.post("/admin/refresh-models")
async def admin_refresh_models(request: Request) -> Dict[str, Any]:
    state = request.app.state.tf
    if not _admin_ok(request):
        return {"ok": False, "error": "unauthorized"}
    try:
        payload = await state.client.get_public_models()
        n = state.registry.upsert_from_upstream(payload)
        return {"ok": True, "count": n, "source": state.registry.source}
    except Exception as e:
        state.registry.last_error = str(e)
        return {"ok": False, "error": str(e)}
