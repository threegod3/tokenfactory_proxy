from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from ..errors import BadRequestError, UpstreamError
from ..models import ChatCompletionRequest, ChatMessage
from ..upstream.client import TokenFactoryClient
from ..upstream.http import UpstreamSession
from ..upstream.protocol import UpstreamDelta
from ..services.models import ModelRegistry
from ..services.tools import parse_json_contract, parse_tool_calls

logger = logging.getLogger(__name__)


def messages_to_prompt(messages: List[ChatMessage]) -> tuple[str, Optional[str]]:
    """Flatten OpenAI messages into (prompt, system_prompt).

    TokenFactory playground is a single-turn chat box, so we serialize the
    conversation history into one prompt while keeping the system message apart.
    """
    system_parts: List[str] = []
    turns: List[str] = []
    for m in messages:
        role = (m.role or "user").lower()
        text = m.text_content()
        if not text:
            continue
        if role in ("system", "developer"):
            system_parts.append(text)
        elif role == "assistant":
            turns.append(f"Assistant: {text}")
        elif role == "tool":
            turns.append(f"Tool({m.name or 'result'}): {text}")
        else:
            turns.append(f"User: {text}")
    system_prompt = "\n\n".join(system_parts).strip() or None
    if len(turns) == 1 and turns[0].startswith("User: "):
        prompt = turns[0][len("User: "):]
    else:
        prompt = "\n\n".join(turns).strip()
    if not prompt:
        raise BadRequestError("messages must contain at least one non-empty message", param="messages")
    return prompt, system_prompt


def conservative_token_estimate(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + other)


class ChatService:
    def __init__(self, session: UpstreamSession, registry: ModelRegistry, pool: Any = None,
                 quota: Any = None):
        self.session = session
        self.registry = registry
        self.pool = pool
        self.quota = quota
        self.client = TokenFactoryClient(session)
        self._conv_lock = asyncio.Lock()
        self._shared_conversation_id: Optional[str] = None
        # 成功创建 turn 即消耗一次上游配额
        self.client.on_turn_created = self._on_turn_created

    def _on_turn_created(self, email: Optional[str]) -> None:
        if self.quota is None or not email:
            return
        try:
            self.quota.increment(email)
        except Exception:
            return
        # 快用完时后台预热后面两个号：换号几乎零等待，浏览器在后台起
        if self.pool is not None and self.quota.remaining(email) <= 3:
            try:
                asyncio.create_task(self.pool.warm_next(
                    quota_remaining=self.quota.remaining, exclude={email}, count=2))
            except Exception:
                pass

    def _current_email(self) -> Optional[str]:
        """活动账号身份；session.email 缺失时按 ACCESS_TOKEN 反查账号池。"""
        if self.session.email:
            return self.session.email
        if self.pool is None:
            return None
        tok = (self.session.cookies or {}).get("ACCESS_TOKEN")
        if not tok:
            return None
        for email, st in getattr(self.pool, "states", {}).items():
            if (st.cookies or {}).get("ACCESS_TOKEN") == tok:
                self.session.email = email
                return email
        return None

    async def _ensure_ready(self) -> None:
        ok = await self.session.ensure_valid_session(self.pool)
        if not ok:
            state = self.session.session_state()
            raise UpstreamError(
                "No valid upstream session. Provide upstream.cookies_file with an authenticated "
                f"TokenFactory session, or configure accounts[] for auto re-login. state={state}",
                upstream_status=401,
            )
        self._current_email()
        # 用上游真实 used 校正本地计数（免费接口），提前换掉已耗尽的号
        await self._sync_quota()
        # 当前账号今日配额用完 → 换下一个号（纯协议优先）
        if (self.quota is not None and self.session.email
                and self.quota.remaining(self.session.email) <= 0
                and self.pool is not None):
            await self.pool.switch_session(
                self.session, exclude={self.session.email},
                quota_remaining=self.quota.remaining)
            await self._sync_quota()
        if not (self.session.tenant_id and self.session.workspace_id):
            tenant, workspace = await self.client.discover_console_context()
            if tenant:
                self.session.tenant_id = tenant
            if workspace:
                self.session.workspace_id = workspace

    @staticmethod
    def _is_quota_error(e: Exception) -> bool:
        if isinstance(e, UpstreamError):
            if e.upstream_status == 429:
                return True
            if "CH-208" in str(getattr(e, "upstream_body", "") or ""):
                return True
        if "CH-208" in str(e) or "429" in str(e):
            return True
        return False

    @staticmethod
    def _is_quota_text(text: Optional[str]) -> bool:
        t = text or ""
        return ("CH-208" in t or " 429" in t or "429:" in t
                or "실행 횟수" in t or "소진" in t)

    async def _sync_quota(self) -> None:
        """以上游真实 used 校正本地计数；满额即标记，供换号使用。"""
        if self.quota is None:
            return
        email = self._current_email()
        if not email:
            return
        try:
            q = await self.client.get_quota()
        except Exception as e:
            logger.debug("quota sync skipped: %s", e)
            return
        if isinstance(q, dict):
            used, limit = q.get("used"), q.get("limit")
            if isinstance(used, int):
                self.quota.set_count(email, used)
                if isinstance(limit, int) and used >= limit:
                    self.quota.mark_exhausted(email)

    async def _chat_once_guarded(self, model_id: str, prompt: str,
                                 extra: Optional[Dict[str, Any]] = None,
                                 system_prompt: Optional[str] = None,
                                 conversation_id: Optional[str] = None):
        """chat_once + 429/配额自动换号重试。

        配额 429 既可能出现在建 turn，也可能出现在 completions SSE 阶段，
        两种都要能触发换号；已输出过文本时不重试，避免内容重复。
        """
        for attempt in range(4):
            emitted = False
            try:
                async for d in self.client.chat_once(prompt=prompt, model_id=model_id,
                                                     system_prompt=system_prompt,
                                                     conversation_id=conversation_id,
                                                     extra=extra or {}):
                    if d.error and not emitted and self._is_quota_text(d.error):
                        raise UpstreamError(d.error, upstream_status=429, upstream_body=d.error)
                    if d.error:
                        yield d
                        return
                    if d.text:
                        emitted = True
                    yield d
                return
            except UpstreamError as e:
                if self._is_quota_error(e) and self.pool is not None and attempt < 3:
                    email = self._current_email()
                    logger.warning("upstream quota exhausted for %s; rotating account", email)
                    if self.quota is not None and email:
                        self.quota.mark_exhausted(email)
                    ok = await self.pool.switch_session(
                        self.session,
                        exclude={email} if email else None,
                        quota_remaining=(self.quota.remaining if self.quota else None))
                    if ok:
                        await self._sync_quota()
                        continue
                    raise UpstreamError(
                        "all accounts exhausted today (daily quota); register more accounts",
                        upstream_status=429) from e
                raise

    async def prepare(self, req: ChatCompletionRequest) -> tuple[str, Optional[str], str]:
        model_id = self.registry.resolve(req.model)
        prompt, system_prompt = messages_to_prompt(req.messages)
        return model_id, system_prompt, prompt

    def _turn_extra(self, req: ChatCompletionRequest) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if req.temperature is not None:
            extra["temperature"] = req.temperature
        if req.top_p is not None:
            extra["topP"] = req.top_p
        if req.max_tokens is not None:
            extra["maxTokens"] = req.max_tokens
        if req.stop is not None:
            extra["stop"] = req.stop
        if req.metadata:
            extra["metadata"] = req.metadata
        if req.extra_body:
            extra.update(req.extra_body)
        return extra

    async def _conversation_id_for(self, req: ChatCompletionRequest) -> Optional[str]:
        if not self.session.cfg.upstream.reuse_conversation:
            return None
        if self._shared_conversation_id:
            return self._shared_conversation_id
        return None

    async def stream(self, req: ChatCompletionRequest) -> AsyncIterator[UpstreamDelta]:
        await self._ensure_ready()
        model_id, system_prompt, prompt = await self.prepare(req)
        conv_id = await self._conversation_id_for(req)
        extra = self._turn_extra(req)
        logger.info("upstream chat: model=%s prompt_len=%d conv=%s", model_id, len(prompt), conv_id or "-")
        try:
            async for delta in self._chat_once_guarded(
                prompt=prompt,
                model_id=model_id,
                system_prompt=system_prompt,
                conversation_id=conv_id,
                extra=extra,
            ):
                yield delta
        except UpstreamError:
            raise
        except Exception as e:
            logger.exception("chat stream failed")
            yield UpstreamDelta(error=f"proxy chat failed: {e}", finish_reason="error", done=True)

    async def complete(self, req: ChatCompletionRequest) -> tuple[str, str, Optional[str]]:
        """Return (text, finish_reason, error)."""
        text_parts: List[str] = []
        finish: Optional[str] = "stop"
        error: Optional[str] = None
        async for d in self.stream(req):
            if d.error:
                error = d.error
                finish = "error"
            if d.text:
                text_parts.append(d.text)
            if d.finish_reason:
                finish = d.finish_reason
            if d.done and not d.text:
                break
        return "".join(text_parts), finish or "stop", error

    # ---- tool-calling pipeline (prompt-level protocol) ----

    async def ensure_ready(self) -> None:
        await self._ensure_ready()

    def build_prompt_openai(self, payload: ChatCompletionRequest) -> tuple[str, Dict[str, Any]]:
        from .tools import build_prompt, normalize_openai_messages
        system, msgs = normalize_openai_messages(payload.messages)
        prompt = build_prompt(system, msgs, payload.tools, payload.tool_choice,
                              compact=self.session.cfg.tools.compact_schemas)
        if not prompt.strip():
            raise BadRequestError("messages must contain at least one non-empty message", param="messages")
        return prompt, self._turn_extra(payload)

    def build_prompt_anthropic(self, body: Dict[str, Any]) -> str:
        from .tools import build_prompt, extract_system_anthropic, normalize_anthropic_messages
        system = extract_system_anthropic(body)
        msgs = normalize_anthropic_messages(body.get("messages") or [])
        prompt = build_prompt(system, msgs, body.get("tools"), body.get("tool_choice"),
                              compact=self.session.cfg.tools.compact_schemas)
        if not prompt.strip():
            raise BadRequestError("messages must contain at least one non-empty message")
        return prompt

    async def run_tools_pipeline(self, model_id: str, prompt: str,
                                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """工具调用流水线。

        策略（实测得出）：
        1. JSON 合同 + settings.response_format=json_object（模型被强制只输出 JSON，最可靠）
        2. 上游拒绝 response_format 时 → 降级为 XML <tool_calls> 块协议
        3. 解析失败时带纠错提示重试一次
        """
        plans: List[Dict[str, Any]] = [{
            "prompt": prompt,
            "extra": {**(extra or {}),
                      "response_format": {"type": "json_object"},
                      "reasoning_effort": "low"},
            "parser": "json",
        }]
        last_full = ""

        for attempt in range(3):
            if attempt >= len(plans):
                break
            plan = plans[attempt]

            parts: List[str] = []
            err: Optional[str] = None
            finish: str = "stop"
            async for d in self._chat_once_guarded(model_id=model_id, prompt=plan["prompt"],
                                                   extra=plan.get("extra") or {}):
                if d.error:
                    err = d.error
                    finish = "error"
                if d.text:
                    parts.append(d.text)
                if d.finish_reason and d.finish_reason != "error":
                    finish = d.finish_reason
                if d.done:
                    break

            if err:
                # 上游不支持 response_format / reasoning_effort → 降级为 XML 块协议
                if (plan.get("parser") == "json" and len(plans) == 1
                        and ("response_format" in err or "Unsupported value" in err
                             or "reasoning" in err.lower())):
                    xml_prompt = self.build_xml_fallback_prompt(prompt)
                    if xml_prompt:
                        logger.info("response_format rejected upstream; falling back to XML tool protocol")
                        plans.append({"prompt": xml_prompt, "extra": extra or {}, "parser": "xml"})
                        continue
                return {"text": "", "calls": [], "finish": "error", "error": err}

            full = "".join(parts)
            last_full = full

            if plan.get("parser") == "json":
                from .tools import parse_model_output
                calls, clean, retryable = parse_model_output(full)
            else:
                calls, clean, retryable = parse_tool_calls(full)

            if retryable and cfg_tools.retry_on_invalid:
                logger.info("tool-call output invalid, retrying with correction")
                plans.append({
                    "prompt": (
                        plan["prompt"]
                        + "\n\n---\n[System]: Your previous reply was:\n"
                        + full[:1200]
                        + '\nThat is not a valid response. Output exactly one JSON object: '
                          '{"type":"tool_calls","calls":[{"name":"...","arguments":{...}}]} '
                          'or {"type":"final","content":"..."} — with no other text.'
                    ),
                    "extra": plan.get("extra") or {},
                    "parser": plan.get("parser", "json"),
                })
                continue

            return {
                "text": clean if not calls else clean.strip(),
                "calls": calls,
                "finish": "tool_calls" if calls else (finish or "stop"),
                "error": None,
            }
        return {"text": last_full, "calls": [], "finish": "stop", "error": None}

    def build_xml_fallback_prompt(self, json_prompt: str) -> Optional[str]:
        """把 JSON 合同换成 XML 块协议（供不支持 response_format 的模型降级）。"""
        from .tools import tool_protocol_block, normalize_tools
        tools = getattr(self, "_current_raw_tools", None)
        if not tools:
            return None
        norm = normalize_tools(tools, compact=self.session.cfg.tools.compact_schemas)
        proto = tool_protocol_block(norm, getattr(self, "_current_tool_choice", None))
        if not proto:
            return None
        # json prompt = [client system] + [contract] + [transcript]；把合同段换成 XML 协议
        tail = json_prompt.split("\n\n", 1)[-1] if "\n\n" in json_prompt else json_prompt
        return proto + "\n\n" + tail

    def set_tool_context(self, tools: Optional[List[Any]], tool_choice: Any = None) -> None:
        self._current_raw_tools = tools
        self._current_tool_choice = tool_choice
