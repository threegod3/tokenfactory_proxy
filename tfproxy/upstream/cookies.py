from __future__ import annotations

import json
import time
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests  # for Netscape-style parsing if needed (kept minimal)

from ..errors import AuthError


def parse_cookie_file(path: str | Path) -> Dict[str, str]:
    """Load cookies from a JSON file.

    Supported formats:
    - {"cookies": [{"name": ..., "value": ...}, ...]}
    - [{"name":..., "value":...}, ...]           (browser extension export array)
    - {"SESSION": "...", "OTHER": "..."}          (flat map)
    - Netscape cookie file (lines starting with #HttpOnly_ or not, tab-separated)
    """
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            cookies_list = data.get("cookies")
            if isinstance(cookies_list, list):
                return {c["name"]: c["value"] for c in cookies_list if isinstance(c, dict) and c.get("name")}
            # flat map of name -> value (values must be strings)
            flat = {k: v for k, v in data.items() if isinstance(v, str)}
            if flat:
                return flat
            return {}
        if isinstance(data, list):
            return {c["name"]: c["value"] for c in data if isinstance(c, dict) and c.get("name")}
        return {}
    # Netscape format fallback
    jar: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            if name:
                jar[name] = value
    return jar


def cookies_from_requests_jar(jar) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for cookie in jar:  # type: ignore[attr-defined]
        out[cookie.name] = cookie.value or ""
    return out


def save_cookies(cookies: Dict[str, str], path: str | Path, url: str = "https://tokenfactory.kt.com/") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": [{"name": k, "value": v, "domain": "tokenfactory.kt.com", "path": "/"} for k, v in cookies.items()],
        "url": url,
        "saved_at": int(time.time()),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def header_cookie_str(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def merge_cookies(base: Optional[Dict[str, str]], incoming: Optional[Iterable[Cookie]]) -> Dict[str, str]:
    merged = dict(base or {})
    for c in incoming or []:
        try:
            merged[c.name] = c.value or ""
        except Exception:
            continue
    return merged
