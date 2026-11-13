from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import httpx

from ..errors import UpstreamError
from .http import UpstreamSession
from .protocol import TurnRef, UpstreamDelta, parse_json_completion, parse_sse_lines, parse_turn_response

logger = logging.getLogger(__name__)

# Observed/known API paths
PATH_PUBLIC_MODELS = "/api/v1/chat/public-models"
PATH_MODEL_FILTER_OPTIONS = "/api/v1/chat/model-filter-options"
PATH_RECOMMENDED_PROMPTS = "/api/v1/chat/recommended-prompts"
PATH_QUOTA = "/api/v1/chat/quota"
PATH_CONVERSATIONS = "/api/v1/chat/conversations"
PATH_CHAT_TURNS = "/api/v1/chat/turns"


class TokenFactoryClient:
    def __init__(self, session: UpstreamSession):
        self.session = session
        self.cfg = session.cfg
        # 上层注入：每成功创建一个 turn（消耗一次上游配额）回调一次
        self.on_turn_created: Any = None

    @property
    def _client(self) -> httpx.AsyncClient:
        assert self.session.client is not None, "UpstreamSession not started"
        return self.session.client

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return path

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(self.session.auth_headers())
        extra = kwargs.pop("headers", None) or {}
        headers.update(extra)
        if "json" in kwargs and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        kwargs["headers"] = headers
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise UpstreamError(f"Upstream request failed: {e}") from e
        self.session.update_cookies_from_response(resp)
        return resp

    async def _json(self, resp: httpx.Response) -> Any:
        text = resp.text
        if resp.status_code >= 400:
            raise UpstreamError(
                f"Upstream HTTP {resp.status_code} for {resp.request.method} {resp.request.url.path}",
                upstream_status=resp.status_code,
                upstream_body=text[:2000],
            )
        try:
            return resp.json()
        except Exception:
            return text

    # ---- discovery ----

    async def discover_console_context(self) -> Tuple[Optional[str], Optional[str]]:
        """Try to discover tenant/workspace IDs from cookies' context or by fetching console."""
        # 1) from playground URL stored in cookies file? not here; try common console entry
        tenant = self.session.tenant_id
        workspace = self.session.workspace_id
        if tenant and workspace:
            return tenant, workspace
        # try /console
        try:
            resp = await self._request("GET", "/console", follow_redirects=True)
            url = str(resp.url)
            m = re.search(r"/console/([^/]+)/workspace/([^/]+)", url)
            if m:
                tenant, workspace = m.group(1), m.group(2)
                self.session.tenant_id, self.session.workspace_id = tenant, workspace
                return tenant, workspace
        except Exception as e:
            logger.debug("console discovery failed: %s", e)
        # try login redirect page? fallback: scan any link in HTML
        try:
            resp = await self._request("GET", "/", follow_redirects=True)
            text = resp.text or ""
            m = re.search(r"/console/([A-Za-z0-9_-]+)/workspace/([A-Za-z0-9_-]+)", text)
            if m:
                tenant, workspace = m.group(1), m.group(2)
                self.session.tenant_id, self.session.workspace_id = tenant, workspace
                return tenant, workspace
        except Exception:
            pass
        return self.session.tenant_id or None, self.session.workspace_id or None

    # ---- models / quota ----

    async def get_public_models(self) -> Dict[str, Any]:
        resp = await self._request("GET", PATH_PUBLIC_MODELS)
        return await self._json(resp)

    async def get_quota(self) -> Any:
        resp = await self._request("GET", PATH_QUOTA)
        if resp.status_code in (401, 403):
            raise UpstreamError("Upstream unauthorized (quota)", upstream_status=resp.status_code, upstream_body=resp.text[:500])
        return await self._json(resp)

    # ---- conversations ----

    async def list_conversations(self, cursor: str = "", size: int = 20) -> Any:
        params = {"cursor": cursor, "size": size}
        resp = await self._request("GET", PATH_CONVERSATIONS, params=params)
        return await self._json(resp)

    # ---- turns / chat ----

    def _build_turn_payload(self, prompt: str, model_id: str, conversation_id: Optional[str] = None,
                            stream: bool = True, system_prompt: Optional[str] = None,
                            extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build the verified TokenFactory turn payload (2026-11 实测)。

        正确形状：
            {
              "content": "...",
              "settings": {...},                 # 只能放顶层；放进 models[] 会让生成失败
              "models": [{"catalogModelId": "...", "stream": true,
                          "systemPrompt": "..."}]  # systemPrompt 只能放 model 条目内
            }
        把 settings 塞进 model 条目时上游会返回 "모델 응답 중 오류가 발생했습니다."
        并且不走 SSE，所以这里严格分层。
        """
        model_entry: Dict[str, Any] = {"catalogModelId": model_id, "stream": bool(stream)}
        if system_prompt:
            model_entry["systemPrompt"] = system_prompt
        payload: Dict[str, Any] = {"content": prompt, "models": [model_entry]}
        if extra:
            settings = {k: v for k, v in extra.items()
                        if k not in ("systemPrompt", "historyMaxTurns")}
            if settings:
                payload["settings"] = settings
            if "historyMaxTurns" in extra:
                payload["historyMaxTurns"] = extra["historyMaxTurns"]
        return payload

    async def create_turn(self, prompt: str, model_id: str, conversation_id: Optional[str] = None,
                          stream: bool = True, system_prompt: Optional[str] = None,
                          extra: Optional[Dict[str, Any]] = None) -> TurnRef:
        body = self._build_turn_payload(prompt, model_id, conversation_id, stream, system_prompt, extra)
        if conversation_id:
            path = f"/api/v1/chat/conversations/{conversation_id}/turns"
        else:
            path = PATH_CHAT_TURNS
        resp = await self._request("POST", path, json=body)
        data = await self._json(resp)
        if logger.isEnabledFor(logging.DEBUG):
            try:
                logger.debug("create_turn response: %s", json.dumps(data, ensure_ascii=False)[:4000])
            except Exception:
                logger.debug("create_turn response (non-json): %r", str(data)[:2000])
        ref = parse_turn_response(data if isinstance(data, dict) else {"raw": data})
        if not ref.conversation_id and conversation_id:
            ref.conversation_id = conversation_id
        if ref.conversation_id and ref.turn_id and self.on_turn_created:
            try:
                self.on_turn_created(self.session.email)
            except Exception:
                pass
        return ref

    async def stream_generation(self, conversation_id: str, turn_id: str, generation_id: str) -> AsyncIterator[UpstreamDelta]:
        path = f"/api/v1/chat/conversations/{conversation_id}/turns/{turn_id}/generations/{generation_id}/completions"
        headers = dict(self.session.auth_headers())
        headers["Accept"] = "text/event-stream, application/json;q=0.9, */*;q=0.8"
        headers["Content-Type"] = "application/json"
        try:
            async with self._client.stream("POST", path, headers=headers, json={}) as resp:
                self.session.update_cookies_from_response(resp)
                ct = (resp.headers.get("content-type") or "").lower()
                if resp.status_code >= 400:
                    body = await resp.aread()
                    text = body.decode("utf-8", errors="replace")
                    yield UpstreamDelta(error=f"Upstream HTTP {resp.status_code}: {text[:500]}", finish_reason="error", done=True, raw=text)
                    return
                if "text/event-stream" in ct:
                    async for delta in self._iter_sse_response(resp):
                        yield delta
                        if delta.done:
                            return
                else:
                    body = await resp.aread()
                    text = body.decode("utf-8", errors="replace")
                    try:
                        obj = json.loads(text)
                    except Exception:
                        obj = text
                    d = parse_json_completion(obj)
                    yield d
                    return
        except httpx.HTTPError as e:
            yield UpstreamDelta(error=f"stream request failed: {e}", finish_reason="error", done=True)

    async def _iter_sse_response(self, resp: httpx.Response) -> AsyncIterator[UpstreamDelta]:
        # feed lines to parser incrementally
        buf: List[str] = []
        async for line in resp.aiter_lines():
            buf.append(line)
            # parse in small batches when blank line arrives (event boundary)
            if line == "":
                for d in parse_sse_lines(buf):
                    yield d
                    if d.done:
                        return
                buf = []
        if buf:
            for d in parse_sse_lines(buf):
                yield d
                if d.done:
                    return

    async def chat_once(self, prompt: str, model_id: str, system_prompt: Optional[str] = None,
                        conversation_id: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> AsyncIterator[UpstreamDelta]:
        """Create a turn and stream completion deltas. Falls back to non-streaming parse."""
        ref = await self.create_turn(prompt, model_id, conversation_id=conversation_id, stream=True,
                                     system_prompt=system_prompt, extra=extra)
        if not ref.conversation_id or not ref.turn_id:
            # Try to surface meaningful error/content from raw response
            raw = ref.raw
            d = parse_json_completion(raw)
            if d.text or d.error:
                yield d
                return
            yield UpstreamDelta(error=f"Upstream did not return conversation/turn ids: {json.dumps(raw, ensure_ascii=False)[:800]}", finish_reason="error", done=True)
            return

        if not ref.generation_ids:
            # Some implementations may not require separate completions call and embed content in turn
            d = parse_json_completion(ref.raw)
            if d.text:
                yield d
                return
            # try to poll turn endpoint once
            try:
                tpath = f"/api/v1/chat/conversations/{ref.conversation_id}/turns/{ref.turn_id}"
                tresp = await self._request("GET", tpath)
                if tresp.status_code < 400:
                    tdata = await self._json(tresp)
                    ref2 = parse_turn_response({"turn": tdata} if isinstance(tdata, dict) else {"raw": tdata})
                    if ref2.generation_ids:
                        ref.generation_ids = ref2.generation_ids
                    else:
                        d2 = parse_json_completion(tdata)
                        if d2.text or d2.error:
                            yield d2
                            return
            except Exception as e:
                logger.debug("turn fetch fallback failed: %s", e)
            if not ref.generation_ids:
                # last resort: return raw
                yield UpstreamDelta(error=f"No generation ids in turn response: {json.dumps(ref.raw, ensure_ascii=False)[:800]}", finish_reason="error", done=True, raw=ref.raw)
                return

        any_text = False
        for gid in ref.generation_ids:
            async for delta in self.stream_generation(ref.conversation_id, ref.turn_id, gid):
                if delta.text:
                    any_text = True
                yield delta
                if delta.done:
                    # if multiple generations, continue? usually one primary; break to avoid duplicates
                    break
            # only stream first generation by default
            break
        if not any_text:
            # no-op safeguard handled by caller
            pass
