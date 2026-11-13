from __future__ import annotations

import logging
from typing import Optional

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)


async def require_api_key(request: Request, authorization: Optional[str] = Header(default=None)) -> str:
    state = getattr(request.app.state, "tf", None)
    if state is None:
        return ""
    cfg = state.cfg
    keys = [k for k in (cfg.server.api_keys or []) if k]
    token = ""
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        else:
            token = authorization.strip()
    if not keys:
        if cfg.server.allow_anonymous:
            return token or "anonymous"
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "No API keys configured on proxy (server.api_keys is empty)", "type": "authentication_error"}},
        )
    if token in keys:
        return token
    if cfg.server.allow_anonymous and not token:
        return "anonymous"
    raise HTTPException(
        status_code=401,
        detail={"error": {"message": "Invalid or missing API key", "type": "authentication_error", "code": "invalid_api_key"}},
    )


async def require_api_key_flexible(request: Request) -> str:
    """同时接受 x-api-key（Anthropic SDK）与 Authorization: Bearer（OpenAI SDK）。"""
    state = getattr(request.app.state, "tf", None)
    if state is None:
        return ""
    cfg = state.cfg
    keys = [k for k in (cfg.server.api_keys or []) if k]
    token = ""
    ak = request.headers.get("x-api-key")
    if ak:
        token = ak.strip()
    if not token:
        auth = request.headers.get("authorization", "")
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not keys:
        if cfg.server.allow_anonymous:
            return token or "anonymous"
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "No API keys configured on proxy (server.api_keys is empty)", "type": "authentication_error"}},
        )
    if token in keys:
        return token
    if cfg.server.allow_anonymous and not token:
        return "anonymous"
    raise HTTPException(
        status_code=401,
        detail={"error": {"message": "Invalid or missing API key", "type": "authentication_error", "code": "invalid_api_key"}},
    )
