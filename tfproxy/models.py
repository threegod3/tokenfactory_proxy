from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---- OpenAI-compatible request/response models ----

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]], None] = None
    name: Optional[str] = None
    tool_calls: Optional[Any] = None
    tool_call_id: Optional[str] = None

    def text_content(self) -> str:
        c = self.content
        if c is None:
            return ""
        if isinstance(c, str):
            return c
        parts: List[str] = []
        for item in c:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif t == "input_text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()


class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    n: Optional[int] = 1
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None
    # tool calling (OpenAI shape); executed via prompt-level protocol
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = None
    # pass-through/extra
    metadata: Optional[Dict[str, Any]] = None
    extra_body: Optional[Dict[str, Any]] = None


class ModelPermission(BaseModel):
    id: str = Field(default_factory=lambda: f"modelperm-{uuid.uuid4().hex[:12]}")
    object: Literal["model_permission"] = "model_permission"
    created: int = Field(default_factory=lambda: int(time.time()))
    allow_create_engine: bool = False
    allow_sampling: bool = True
    allow_logprobs: bool = True
    allow_search_indices: bool = False
    allow_view: bool = True
    allow_fine_tuning: bool = False
    organization: str = "*"
    group: Optional[str] = None
    is_blocking: bool = False


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "tokenfactory"
    permission: List[ModelPermission] = Field(default_factory=lambda: [ModelPermission()])
    root: Optional[str] = None
    parent: Optional[str] = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelCard]


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"
    logprobs: Optional[Any] = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)
    system_fingerprint: Optional[str] = "tfproxy"


class ToolCallDelta(BaseModel):
    index: int = 0
    id: Optional[str] = None
    type: Optional[str] = "function"
    function: Optional[Dict[str, Any]] = None


class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCallDelta]] = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None
    logprobs: Optional[Any] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChunkChoice]
    system_fingerprint: Optional[str] = "tfproxy"


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"
