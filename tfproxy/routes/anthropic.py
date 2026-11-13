"""Anthropic Messages API 兼容层（/v1/messages）。

让 Claude Code 等使用 Anthropic 协议的客户端直接接入：
    ANTHROPIC_BASE_URL=http://127.0.0.1:8000
    ANTHROPIC_API_KEY=sk-tf-local-1

支持 text / tool_use / tool_result，流式与非流式。
工具调用同样走代理层的 prompt 协议（<tool_calls> 块）。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import HTTPException

from ..errors import BadRequestError, ProxyError, UpstreamError
from ..security import require_api_key_flexible
from ..services.tools import estimate_tokens

logger = logging.getLogger(__name__)

router = APIRouter()


def _anthropic_error(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"type": "error", "error": {"type": err_type, "message": message}})


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex


def _build_message_payload(mid: str, model_id: str, res: Dict[str, Any],
                           input_tokens: int) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = []
    if res.get("text"):
        content.append({"type": "text", "text": res["text"]})
    for c in res.get("calls") or []:
        content.append({"type": "tool_use", "id": c["id"], "name": c["name"],
                        "input": c.get("arguments_obj") or {}})
    if not content:
        content = [{"type": "text", "text": ""}]
    if res.get("calls"):
        stop = "tool_use"
    elif res.get("finish") == "length":
        stop = "max_tokens"
    else:
        stop = "end_turn"
    out_text = res.get("text") or ""
    out_tokens = estimate_tokens(out_text) + sum(estimate_tokens(c["arguments"]) for c in res.get("calls") or [])
    return {
        "id": mid,
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": max(1, input_tokens), "output_tokens": max(1, out_tokens)},
    }


@router.post("/v1/messages")
async def create_message(request: Request):
    try:
        await require_api_key_flexible(request)
    except HTTPException as e:
        detail = e.detail
        msg = detail.get("error", {}).get("message", "unauthorized") if isinstance(detail, dict) else str(detail)
        return _anthropic_error(e.status_code, "authentication_error", msg)

    state = request.app.state.tf
    chat = state.chat
    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "invalid_request_error", "invalid JSON body")

    model_id = state.registry.resolve(str(body.get("model") or ""))
    stream = bool(body.get("stream"))
    mid = _msg_id()
    tools = body.get("tools") or []

    try:
        await chat.ensure_ready()
        chat.set_tool_context(tools, body.get("tool_choice"))
        prompt = chat.build_prompt_anthropic(body)
    except BadRequestError as e:
        return _anthropic_error(400, "invalid_request_error", e.message)
    except UpstreamError as e:
        return _anthropic_error(502, "api_error", e.message)

    input_est = estimate_tokens(prompt)

    try:
        res = await chat.run_tools_pipeline(model_id, prompt, {})
    except UpstreamError as e:
        return _anthropic_error(502, "api_error", e.message)
    except Exception as e:
        logger.exception("anthropic message failed")
        return _anthropic_error(500, "api_error", str(e))
    if res.get("error"):
        return _anthropic_error(502, "api_error", res["error"])

    if not stream:
        return JSONResponse(_build_message_payload(mid, model_id, res, input_est))

    # ---- streaming (buffered upstream, protocol-correct event sequence) ----
    payload = _build_message_payload(mid, model_id, res, input_est)

    async def events():
        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {**payload, 'content': []}}, ensure_ascii=False)}\n\n"
        index = 0
        text = res.get("text") or ""
        if text:
            yield (f"event: content_block_start\ndata: "
                   f"{json.dumps({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n")
            step = 220
            for i in range(0, len(text), step):
                piece = text[i:i + step]
                yield (f"event: content_block_delta\ndata: "
                       f"{json.dumps({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': piece}}, ensure_ascii=False)}\n\n")
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index}, ensure_ascii=False)}\n\n"
            index += 1
        for c in res.get("calls") or []:
            block = {"type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}
            yield (f"event: content_block_start\ndata: "
                   f"{json.dumps({'type': 'content_block_start', 'index': index, 'content_block': block}, ensure_ascii=False)}\n\n")
            args_json = json.dumps(c.get("arguments_obj") or {}, ensure_ascii=False)
            yield (f"event: content_block_delta\ndata: "
                   f"{json.dumps({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': args_json}}, ensure_ascii=False)}\n\n")
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index}, ensure_ascii=False)}\n\n"
            index += 1
        yield (f"event: message_delta\ndata: "
               f"{json.dumps({'type': 'message_delta', 'delta': {'stop_reason': payload['stop_reason'], 'stop_sequence': None}, 'usage': {'output_tokens': payload['usage']['output_tokens']}}, ensure_ascii=False)}\n\n")
        yield "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        await require_api_key_flexible(request)
    except HTTPException as e:
        return _anthropic_error(e.status_code, "authentication_error", "unauthorized")
    state = request.app.state.tf
    try:
        body = await request.json()
        prompt = state.chat.build_prompt_anthropic(body)
        return JSONResponse({"input_tokens": max(1, estimate_tokens(prompt))})
    except Exception as e:
        return JSONResponse({"input_tokens": 1})
