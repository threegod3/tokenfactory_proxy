from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..errors import BadRequestError, NotFoundError, UpstreamError
from ..models import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    DeltaMessage,
    ModelCard,
    ModelList,
    Usage,
    new_completion_id,
)
from ..security import require_api_key
from ..services.tools import estimate_tokens, to_openai_tool_calls

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request, _: str = Depends(require_api_key)) -> ModelList:
    state = request.app.state.tf
    reg = state.registry
    data: List[ModelCard] = []
    for m in reg.list():
        data.append(ModelCard(id=m["id"], owned_by=m.get("owned_by", "tokenfactory")))
    if not any(m.id == "auto" for m in data):
        data.insert(0, ModelCard(id="auto", owned_by="kt"))
    return ModelList(data=data)


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, request: Request, _: str = Depends(require_api_key)) -> ModelCard:
    state = request.app.state.tf
    resolved = state.registry.resolve(model_id)
    meta = state.registry.get(resolved)
    if not meta and resolved != "auto":
        raise NotFoundError(f"model '{model_id}' not found")
    return ModelCard(id=meta["id"] if meta else resolved, owned_by=(meta or {}).get("owned_by", "tokenfactory"))


def _usage(prompt_est: int, completion_est: int) -> Usage:
    return Usage(prompt_tokens=prompt_est, completion_tokens=completion_est,
                 total_tokens=prompt_est + completion_est)


@router.post("/v1/chat/completions")
async def chat_completions(payload: ChatCompletionRequest, request: Request, _: str = Depends(require_api_key)):
    state = request.app.state.tf
    chat = state.chat
    model_id = state.registry.resolve(payload.model)
    stream = bool(payload.stream)
    cmpl_id = new_completion_id()
    created = int(time.time())

    if not payload.messages:
        raise BadRequestError("messages is required", param="messages")

    tools_active = bool(payload.tools) and state.cfg.tools.enabled

    # ---------- tool-calling path (buffered) ----------
    if tools_active:
        await chat.ensure_ready()
        chat.set_tool_context(payload.tools, payload.tool_choice)
        prompt, extra = chat.build_prompt_openai(payload)
        prompt_est = estimate_tokens(prompt)
        res = await chat.run_tools_pipeline(model_id, prompt, extra)
        if res["error"]:
            raise UpstreamError(res["error"], upstream_status=502, upstream_body={"model": model_id})
        calls = res["calls"]
        text = res["text"] or ""
        finish = res["finish"] or "stop"
        oai_calls = to_openai_tool_calls(calls) if calls else None

        if not stream:
            message = {"role": "assistant", "content": text or None}
            if oai_calls:
                message["tool_calls"] = oai_calls
            resp = ChatCompletionResponse(
                id=cmpl_id, created=created, model=model_id,
                choices=[ChatCompletionChoice(index=0, message=message, finish_reason=finish)],
                usage=_usage(prompt_est, estimate_tokens(text)),
            )
            return JSONResponse(resp.model_dump())

        async def tool_event_stream():
            first = ChatCompletionChunk(
                id=cmpl_id, created=created, model=model_id,
                choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(role="assistant", content=""))],
            )
            yield f"data: {json.dumps(first.model_dump(), ensure_ascii=False)}\n\n"
            # content part (usually empty when tool calls happen)
            buf = text
            step = 220
            for i in range(0, len(buf), step):
                chunk = ChatCompletionChunk(
                    id=cmpl_id, created=created, model=model_id,
                    choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(content=buf[i:i + step]))],
                )
                yield f"data: {json.dumps(chunk.model_dump(), ensure_ascii=False)}\n\n"
            if oai_calls:
                # full arguments in a single delta is valid per OpenAI spec
                tc_chunk = ChatCompletionChunk(
                    id=cmpl_id, created=created, model=model_id,
                    choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(tool_calls=[
                        {"index": i, "id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for i, c in enumerate(calls)]))],
                )
                yield f"data: {json.dumps(tc_chunk.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"
            last = ChatCompletionChunk(
                id=cmpl_id, created=created, model=model_id,
                choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(), finish_reason=finish)],
            )
            yield f"data: {json.dumps(last.model_dump(), ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(tool_event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    # ---------- plain chat path ----------
    if not stream:
        text, finish, error = await chat.complete(payload)
        if error:
            raise UpstreamError(error, upstream_status=502, upstream_body={"model": model_id})
        prompt_tokens = sum(len(m.text_content()) for m in payload.messages) // 4 or 1
        completion_tokens = max(1, len(text) // 4) if text else 0
        resp = ChatCompletionResponse(
            id=cmpl_id,
            created=created,
            model=model_id,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message={"role": "assistant", "content": text},
                    finish_reason=finish or "stop",
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
        return JSONResponse(resp.model_dump())

    async def event_stream():
        first = ChatCompletionChunk(
            id=cmpl_id,
            created=created,
            model=model_id,
            choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(role="assistant", content=""))],
        )
        yield f"data: {json.dumps(first.model_dump(), ensure_ascii=False)}\n\n"
        error_text = None
        finish = "stop"
        async for d in chat.stream(payload):
            if d.error:
                error_text = d.error
                finish = "error"
            if d.text:
                chunk = ChatCompletionChunk(
                    id=cmpl_id,
                    created=created,
                    model=model_id,
                    choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(content=d.text))],
                )
                yield f"data: {json.dumps(chunk.model_dump(), ensure_ascii=False)}\n\n"
            if d.finish_reason:
                finish = d.finish_reason
            if d.done:
                break
        if error_text:
            err = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                "error": {"message": error_text, "type": "upstream_error"},
            }
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        last = ChatCompletionChunk(
            id=cmpl_id,
            created=created,
            model=model_id,
            choices=[ChatCompletionChunkChoice(index=0, delta=DeltaMessage(), finish_reason=finish)],
        )
        yield f"data: {json.dumps(last.model_dump(), ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/v1/quota")
async def quota(request: Request, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    state = request.app.state.tf
    try:
        data = await state.client.get_quota()
        return {"ok": True, "quota": data}
    except UpstreamError as e:
        return {"ok": False, "error": e.message, "upstream_status": e.upstream_status, "body": e.upstream_body}
