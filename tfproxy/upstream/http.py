from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, Optional

import httpx

from ..config import AppConfig, UpstreamConfig
from ..errors import UpstreamError
from .cookies import cookies_from_requests_jar, parse_cookie_file, save_cookies

logger = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


def build_async_client(cfg: UpstreamConfig) -> httpx.AsyncClient:
    proxies = cfg.proxy or None
    timeout = httpx.Timeout(
        connect=cfg.connect_timeout_seconds,
        read=cfg.read_timeout_seconds,
        write=60,
        pool=60,
    )
    return httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/"),
        proxy=proxies,
        timeout=timeout,
        verify=cfg.verify_tls,
        follow_redirects=False,
        http2=False,
    )


class UpstreamSession:
    """Holds cookies, tenant/workspace context and shared httpx client."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.ucfg = cfg.upstream
        self.client: Optional[httpx.AsyncClient] = None
        self.cookies: Dict[str, str] = {}
        self.tenant_id: str = cfg.upstream.tenant_id
        self.workspace_id: str = cfg.upstream.workspace_id
        self.email: Optional[str] = None
        self.session_loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.client is None:
            self.client = build_async_client(self.ucfg)
        await self.load_cookies()

    async def aclose(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

    async def load_cookies(self) -> None:
        cf = self.cfg.resolve_path(self.ucfg.cookies_file)
        if cf and cf.exists():
            self.cookies = parse_cookie_file(cf)
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if data.get("email"):
                        self.email = str(data["email"])
                    if data.get("tenant_id"):
                        self.tenant_id = str(data["tenant_id"])
                    if data.get("workspace_id"):
                        self.workspace_id = str(data["workspace_id"])
            except Exception:
                pass
        self.session_loaded_at = time.time()
        self.derive_context_from_profile()
        if self.ucfg.cookies_save_file and self.cookies:
            sf = self.cfg.resolve_path(self.ucfg.cookies_save_file)
            if sf and sf != cf:
                save_cookies(self.cookies, sf)

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        self.cookies = dict(cookies or {})
        self.derive_context_from_profile()
        sf = self.cfg.resolve_path(self.ucfg.cookies_save_file) if self.ucfg.cookies_save_file else None
        if sf:
            save_cookies(self.cookies, sf)

    def update_cookies_from_response(self, response: httpx.Response) -> None:
        try:
            jar = response.cookies.jar  # type: ignore[attr-defined]
        except Exception:
            return
        changed = False
        for c in jar:
            if c.name and c.value:
                self.cookies[c.name] = c.value
                changed = True
        if changed and self.ucfg.cookies_save_file:
            sf = self.cfg.resolve_path(self.ucfg.cookies_save_file)
            if sf:
                save_cookies(self.cookies, sf)

    def auth_headers(self, playground: bool = True) -> Dict[str, str]:
        base = self.ucfg.base_url.rstrip("/")
        referer = base + "/"
        tenant = self.tenant_id
        workspace = self.workspace_id
        if playground and tenant and workspace:
            referer = f"{base}/console/{tenant}/workspace/{workspace}/playground"
        hdrs: Dict[str, str] = {
            "User-Agent": self.ucfg.user_agent,
            "Accept": "*/*",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8,zh-CN;q=0.7",
            "Origin": base,
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        # TokenFactory gateway requires these two headers on every API call
        if tenant:
            hdrs["tf-tenant-id"] = tenant
        if workspace:
            hdrs["tf-space-id"] = workspace
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs.update({k: v for k, v in self.ucfg.extra_headers.items()})
        return hdrs

    def derive_context_from_profile(self) -> None:
        """tenant/workspace can be recovered from the tf_auth_profile cookie."""
        raw = self.cookies.get("tf_auth_profile")
        if not raw:
            return
        try:
            from urllib.parse import unquote
            import json as _json

            prof = _json.loads(unquote(raw))
        except Exception:
            return
        if not self.tenant_id and prof.get("defaultTenantId"):
            self.tenant_id = str(prof["defaultTenantId"])
        if not self.workspace_id and prof.get("defaultWorkspaceId"):
            self.workspace_id = str(prof["defaultWorkspaceId"])
        # 身份必须能从 cookie 自证：否则配额记账/换号会以 email=None 运行
        if not self.email and prof.get("email"):
            self.email = str(prof["email"])

    def is_logged_in_heuristic(self) -> bool:
        # TokenFactory console typically requires some session cookie(s);
        # a precise check is performed via /api/v1/chat/quota on health-check.
        return bool(self.cookies)

    # ---- token expiry / refresh (pure protocol, no browser) ----

    def access_token_expires_at(self) -> Optional[float]:
        """ACCESS_TOKEN_EXPIRES_AT is epoch milliseconds (as a string)."""
        raw = self.cookies.get("ACCESS_TOKEN_EXPIRES_AT")
        if not raw:
            return None
        try:
            v = float(raw)
        except Exception:
            return None
        # normalize: ms vs s
        return v / 1000.0 if v > 1e11 else v

    def refresh_token_expires_at(self) -> Optional[float]:
        raw = self.cookies.get("REFRESH_TOKEN_EXPIRES_AT")
        if not raw:
            return None
        try:
            v = float(raw)
        except Exception:
            return None
        return v / 1000.0 if v > 1e11 else v

    def session_state(self) -> Dict[str, Any]:
        now = time.time()
        acc = self.access_token_expires_at()
        ref = self.refresh_token_expires_at()
        return {
            "has_cookies": bool(self.cookies),
            "access_expires_in": round(acc - now, 1) if acc else None,
            "refresh_expires_in": round(ref - now, 1) if ref else None,
            "access_valid": bool(acc and acc > now),
            "refresh_valid": bool(ref and ref > now),
        }

    def access_token_valid(self, leeway_seconds: int = 60) -> bool:
        acc = self.access_token_expires_at()
        if acc is None:
            # unknown expiry: assume the cookie itself is enough
            return bool(self.cookies.get("ACCESS_TOKEN"))
        return acc > (time.time() + leeway_seconds)

    def refresh_token_valid(self) -> bool:
        ref = self.refresh_token_expires_at()
        if ref is None:
            return bool(self.cookies.get("REFRESH_TOKEN"))
        return ref > time.time()

    async def refresh_access_token(self) -> bool:
        """Renew ACCESS_TOKEN via POST /internal/auth/refresh.

        Verified: returns 204 and re-issues the whole cookie set.
        Pure protocol -- no browser involved.
        """
        if not self.refresh_token_valid():
            logger.warning("refresh token expired or missing; cannot refresh")
            return False
        client = self.client
        if client is None:
            return False
        headers = self.auth_headers(playground=False)
        headers["Content-Type"] = "application/json"
        try:
            resp = await client.post("/internal/auth/refresh", headers=headers, json={})
        except httpx.HTTPError as e:
            logger.warning("refresh request failed: %s", e)
            return False
        updated = self._collect_set_cookies(resp)
        if resp.status_code in (200, 204) and updated.get("ACCESS_TOKEN"):
            self.cookies.update(updated)
            self.derive_context_from_profile()
            self._persist_cookies()
            logger.info("access token refreshed (now valid for %ss)",
                        round((self.access_token_expires_at() or 0) - time.time(), 1))
            return True
        logger.warning("refresh rejected: HTTP %s %s", resp.status_code, resp.text[:200])
        return False

    @staticmethod
    def _collect_set_cookies(resp: httpx.Response) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            for c in resp.cookies.jar:  # type: ignore[attr-defined]
                if c.name and c.value:
                    out[c.name] = c.value
        except Exception:
            pass
        # httpx may collapse duplicates; also scan raw headers
        try:
            for raw in resp.headers.get_list("set-cookie"):
                first = raw.split(";", 1)[0]
                if "=" in first:
                    k, v = first.split("=", 1)
                    k = k.strip()
                    if k:
                        out[k] = v.strip()
        except Exception:
            pass
        return out

    def _persist_cookies(self) -> None:
        sf = self.cfg.resolve_path(self.ucfg.cookies_save_file) if self.ucfg.cookies_save_file else None
        if sf:
            save_cookies(self.cookies, sf)

    async def ensure_valid_session(self, pool: Any = None) -> bool:
        """Make sure we hold a usable ACCESS_TOKEN.

        Order: still-valid -> refresh with REFRESH_TOKEN -> full login (needs browser for recaptcha).
        """
        if not self.cookies:
            await self.load_cookies()
        if self.access_token_valid():
            return True
        if await self.refresh_access_token():
            return True
        if pool is not None:
            logger.info("refresh failed, falling back to full login")
            self.cookies = {}
            if await pool.ensure_session(self):
                return True
        return self.access_token_valid()

    async def maybe_login(self, pool: Any = None) -> None:
        """If configured, attempt login via account pool."""
        if pool is None:
            return
        await pool.ensure_session(self)


SESSION_SEMAPHORE = asyncio.Semaphore(4)
