from __future__ import annotations

import base64
import atexit
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..config import AppConfig
from ..errors import AuthError, UpstreamError

logger = logging.getLogger(__name__)

# 每个线程复用一台常驻 Chrome：换号时不再冷启动（20s → 5~10s），
# 同时减少新上下文数量，避免 reCAPTCHA 评分被拉低。
_TLS = threading.local()


def _close_tls_browser() -> None:
    browser = getattr(_TLS, "browser", None)
    page = getattr(_TLS, "page", None)
    pw = getattr(_TLS, "pw", None)
    try:
        if page is not None and not page.is_closed():
            page.close()
    except Exception:
        pass
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass
    for name in ("browser", "page", "pw", "proxy", "signature"):
        setattr(_TLS, name, None)


atexit.register(_close_tls_browser)


def _get_recaptcha_page(proxy: Optional[str], timeout_s: int, chrome: str):
    """返回该线程内已就绪的 page（已加载 /login 且 grecaptcha 可用）。"""
    from playwright.sync_api import sync_playwright  # type: ignore

    signature = (proxy, chrome)
    if getattr(_TLS, "pw", None) is None or getattr(_TLS, "signature", None) != signature:
        _close_tls_browser()
        _TLS.pw = sync_playwright().start()
        _TLS.signature = signature

    browser = getattr(_TLS, "browser", None)
    if browser is None or not browser.is_connected():
        launch_kwargs: Dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled",
                     "--no-first-run", "--no-default-browser-check", "--disable-gpu"],
        }
        if os.path.exists(chrome):
            launch_kwargs["executable_path"] = chrome
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = _TLS.pw.chromium.launch(**launch_kwargs)
        _TLS.browser = browser
        _TLS.page = None

    page = getattr(_TLS, "page", None)
    if page is None or page.is_closed():
        page = browser.new_page()
        page.goto("https://tokenfactory.kt.com/login", wait_until="domcontentloaded",
                  timeout=timeout_s * 1000)
        page.wait_for_function(
            "() => window.grecaptcha && window.grecaptcha.enterprise "
            "&& typeof window.grecaptcha.enterprise.execute === 'function'",
            timeout=timeout_s * 1000,
        )
        _TLS.page = page
    return page


def get_recaptcha_token_sync(site_key: Optional[str] = None, mode: str = "playwright",
                             action: str = "login", timeout_s: int = 180,
                             proxy: Optional[str] = None) -> Optional[str]:
    """Best-effort sync recaptcha token acquisition.

    mode=playwright: reuse a per-thread Chrome, execute grecaptcha.enterprise in-page.
    mode=manual: read TFPROXY_RECAPTCHA_TOKEN from env.
    mode=disabled: return None.
    """
    if mode == "disabled":
        return None
    if mode == "manual":
        tok = os.getenv("TFPROXY_RECAPTCHA_TOKEN")
        return tok.strip() if tok else None
    try:
        from playwright.sync_api import sync_playwright  # type: ignore  # noqa: F401
    except Exception as e:
        logger.warning("Playwright not available for recaptcha: %s", e)
        return None
    site_keys = [site_key] if site_key else [
        "6Ld6CAEuAAAAABMcfRzFiXfSsiPjCgehCytp1laq",
        "6LeZEBcuAAAAAMQOk0zy4wrzZPzUbBSeHRnD22eM",
    ]
    chrome = os.getenv("TFPROXY_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    last_err = None
    # playwright 只认 socks5://，不接受 socks5h://
    if proxy:
        proxy = proxy.replace("socks5h://", "socks5://")
    for attempt in range(2):
        try:
            page = _get_recaptcha_page(proxy, timeout_s, chrome)
            for sk in site_keys:
                try:
                    token = page.evaluate(
                        """async (a) => {
                             await new Promise(r => grecaptcha.enterprise.ready(r));
                             return await grecaptcha.enterprise.execute(a.sk, {action: a.action});
                           }""",
                        {"sk": sk, "action": action},
                    )
                    if token and isinstance(token, str) and len(token) > 30:
                        return token
                except Exception as e:
                    last_err = str(e)
        except Exception as e:
            last_err = str(e)
        # 复用页可能已失效：关掉重建后重试一次
        try:
            page = getattr(_TLS, "page", None)
            if page is not None and not page.is_closed():
                page.close()
        except Exception:
            pass
        _TLS.page = None
    logger.warning("Failed to obtain recaptcha token: %s", last_err)
    return None


def encrypt_password_rsa_oaep(public_key_pem: str, password: str) -> str:
    pem = public_key_pem.replace("\\n", "\n").strip()
    if not pem.startswith("-----BEGIN"):
        pem = "-----BEGIN PUBLIC KEY-----\n" + pem + "\n-----END PUBLIC KEY-----"
    pub = serialization.load_pem_public_key(pem.encode("utf-8"))
    ct = pub.encrypt(
        password.encode("utf-8"),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ct).decode("ascii")


@dataclass
class LoginResult:
    ok: bool
    cookies: Dict[str, str]
    response: Any = None
    status_code: Optional[int] = None
    error: Optional[str] = None


def fetch_login_public_key(base_url: str, session: Optional[requests.Session] = None, proxies: Optional[dict] = None, timeout: int = 30) -> str:
    s = session or requests.Session()
    url = base_url.rstrip("/") + "/api/v1/account/login/public-key"
    resp = s.get(url, proxies=proxies, timeout=timeout)
    if resp.status_code >= 400:
        raise UpstreamError(f"Failed to fetch login public key: {resp.status_code}", upstream_status=resp.status_code, upstream_body=resp.text[:500])
    # response may be {"publicKey":"..."} or plain string
    try:
        data = resp.json()
    except Exception:
        text = resp.text.strip().strip('"')
        if "BEGIN PUBLIC KEY" in text or len(text) > 100:
            return text
        raise UpstreamError("Unexpected public-key response", upstream_status=resp.status_code, upstream_body=resp.text[:500])
    if isinstance(data, dict):
        for k in ("publicKey", "public_key", "key", "data"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for kk in ("publicKey", "public_key", "key"):
                    if isinstance(v.get(kk), str):
                        return v[kk]
    if isinstance(data, str):
        return data
    raise UpstreamError("Could not parse public key from response", upstream_status=resp.status_code, upstream_body=data)


def login_sync(
    base_url: str,
    email: str,
    password: str,
    recaptcha_token: Optional[str] = None,
    recaptcha_mode: str = "playwright",
    proxies: Optional[dict] = None,
    timeout: int = 40,
) -> LoginResult:
    """纯协议登录：浏览器只提供 reCAPTCHA token，POST 由本函数完成。

    协议（已实测）：
        GET  /api/v1/auth/encryption-key  -> {keyPairId, publicKey(JWK)}
        POST /internal/auth/login
             {email, recaptchaToken, keyPairId, encryptedPassword}
        -> 200 {"ok":true} + Set-Cookie ACCESS_TOKEN / REFRESH_TOKEN / tf_auth_profile
    """
    s = requests.Session()
    if proxies:
        s.proxies.update(proxies)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": base_url.rstrip("/"),
        "Referer": base_url.rstrip("/") + "/login",
    })

    # 1) encryption key (RSA JWK, not PEM)
    try:
        resp = s.get(base_url.rstrip("/") + "/api/v1/auth/encryption-key", timeout=timeout)
        ek = resp.json()
        key_pair_id = ek["keyPairId"]
        jwk = ek["publicKey"]
    except Exception as e:
        return LoginResult(ok=False, cookies={}, error=f"encryption-key failed: {e}")

    # 2) RSA-OAEP-256 with JWK
    try:
        import base64 as _b64
        from cryptography.hazmat.primitives import hashes as _h
        from cryptography.hazmat.primitives.asymmetric import padding as _p, rsa as _rsa

        def _b64u(v: str) -> bytes:
            v = v.replace("-", "+").replace("_", "/")
            return _b64.b64decode(v + "=" * (-len(v) % 4))

        n = int.from_bytes(_b64u(jwk["n"]), "big")
        e = int.from_bytes(_b64u(jwk["e"]), "big")
        pub = _rsa.RSAPublicNumbers(e, n).public_key()
        ct = pub.encrypt(password.encode(), _p.OAEP(
            mgf=_p.MGF1(algorithm=_h.SHA256()), algorithm=_h.SHA256(), label=None))
        enc = _b64.b64encode(ct).decode()
    except Exception as e:
        return LoginResult(ok=False, cookies={}, error=f"password encrypt failed: {e}")

    # 3) recaptcha token (browser needed here only)
    if not recaptcha_token and recaptcha_mode != "disabled":
        proxy_url = None
        if proxies:
            proxy_url = proxies.get("https") or proxies.get("http")
        recaptcha_token = get_recaptcha_token_sync(mode=recaptcha_mode, action="login",
                                                   proxy=proxy_url)
    if not recaptcha_token:
        return LoginResult(ok=False, cookies={}, error="no recaptcha token available")

    # 4) login
    payload = {
        "email": email,
        "recaptchaToken": recaptcha_token,
        "keyPairId": key_pair_id,
        "encryptedPassword": enc,
    }
    url = base_url.rstrip("/") + "/internal/auth/login"
    try:
        resp = s.post(url, json=payload, timeout=timeout)
    except Exception as e:
        return LoginResult(ok=False, cookies={}, error=f"login request failed: {e}")

    cookies = {c.name: c.value for c in s.cookies}
    ok = resp.status_code < 400 and bool(cookies.get("ACCESS_TOKEN"))
    err = None
    if not ok:
        try:
            err = json.dumps(resp.json(), ensure_ascii=False)[:500]
        except Exception:
            err = resp.text[:500]
    body = None
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:1000]
    return LoginResult(ok=ok, cookies=cookies, response=body,
                       status_code=resp.status_code, error=err)
