from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TurnRef:
    conversation_id: str
    turn_id: str
    generation_ids: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UpstreamDelta:
    text: str = ""
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    raw: Any = None
    done: bool = False


def extract_generation_ids(turn_obj: Dict[str, Any]) -> List[str]:
    gens = turn_obj.get("generations") or []
    ids: List[str] = []
    for g in gens:
        if isinstance(g, dict):
            gid = g.get("id") or g.get("generationId") or g.get("generation_id")
            if gid:
                ids.append(str(gid))
        elif isinstance(g, str):
            ids.append(g)
    # sometimes nested differently
    if not ids:
        for key in ("generationId", "generation_id"):
            if turn_obj.get(key):
                ids.append(str(turn_obj[key]))
    return ids


def parse_turn_response(data: Dict[str, Any]) -> TurnRef:
    # Shape variants:
    # { conversation: {id,...}, turn: {id, generations:[{id}..], ...} }
    # { conversationId, turnId, generations }
    # { data: {...} }
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    conv = data.get("conversation") or {}
    turn = data.get("turn") or {}
    conv_id = (
        (conv.get("id") if isinstance(conv, dict) else None)
        or data.get("conversationId")
        or data.get("conversation_id")
        or ""
    )
    turn_id = (
        (turn.get("id") if isinstance(turn, dict) else None)
        or data.get("turnId")
        or data.get("turn_id")
        or ""
    )
    gen_source = turn if isinstance(turn, dict) and turn.get("generations") is not None else data
    gen_ids = extract_generation_ids(gen_source if isinstance(gen_source, dict) else {})
    return TurnRef(
        conversation_id=str(conv_id),
        turn_id=str(turn_id),
        generation_ids=gen_ids,
        raw=data,
    )


def _extract_text_from_obj(obj: Any) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (delta_text, finish_reason, error_message)."""
    if obj is None:
        return "", None, None
    if isinstance(obj, str):
        # could be plain text chunk
        return obj, None, None
    if not isinstance(obj, dict):
        return "", None, None

    # error/status
    status = obj.get("status") or obj.get("state")
    err = obj.get("errorMessage") or obj.get("error") or obj.get("message")
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err, ensure_ascii=False)
    finish = obj.get("finish_reason") or obj.get("finishReason")
    if status in ("failed", "error") or (err and status in (None, "failed", "error")):
        return "", finish or "error", str(err) if err else "upstream error"
    if status in ("completed", "done", "succeeded", "success"):
        finish = finish or "stop"

    # common text locations
    # 1) delta.content / delta.text
    delta = obj.get("delta")
    if isinstance(delta, dict):
        for k in ("content", "text", "output_text"):
            v = delta.get(k)
            if isinstance(v, str) and v:
                return v, finish, None
    if isinstance(delta, str) and delta:
        return delta, finish, None

    # 2) choices[0].delta.content (OpenAI-like)
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] or {}
        if isinstance(c0, dict):
            d = c0.get("delta") or {}
            if isinstance(d, dict):
                t = d.get("content") or d.get("text")
                if isinstance(t, str) and t:
                    return t, c0.get("finish_reason") or finish, None
            msg = c0.get("message") or {}
            if isinstance(msg, dict):
                t = msg.get("content")
                if isinstance(t, str) and t:
                    return t, c0.get("finish_reason") or finish, None
            t = c0.get("text")
            if isinstance(t, str) and t:
                return t, c0.get("finish_reason") or finish, None
            # terminal chunk: delta empty but finish_reason set
            if c0.get("finish_reason"):
                return "", c0.get("finish_reason"), None

    # 3) direct content/text fields (full or incremental)
    for k in ("content", "text", "output_text", "token", "chunk"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v, finish, None
        if isinstance(v, dict):
            t = v.get("text") or v.get("content")
            if isinstance(t, str) and t:
                return t, finish, None

    # 4) message.content
    msg = obj.get("message")
    if isinstance(msg, dict):
        t = msg.get("content")
        if isinstance(t, str) and t:
            return t, finish, None

    # 5) generation/result nesting
    for k in ("generation", "result", "data", "turn", "output"):
        nested = obj.get(k)
        if isinstance(nested, dict):
            t, f2, e2 = _extract_text_from_obj(nested)
            if t or e2:
                return t, f2 or finish, e2
        if isinstance(nested, list):
            for item in nested:
                t, f2, e2 = _extract_text_from_obj(item)
                if t or e2:
                    return t, f2 or finish, e2

    # completion marker with no text
    if status in ("completed", "done", "succeeded", "success"):
        return "", finish or "stop", None
    return "", finish, None


def parse_sse_lines(lines: Iterable[str]) -> Iterable[UpstreamDelta]:
    """Parse an iterable of decoded SSE lines into UpstreamDelta events."""
    event_name: Optional[str] = None
    data_lines: List[str] = []
    last_full_text = ""

    def flush() -> Optional[UpstreamDelta]:
        nonlocal event_name, data_lines, last_full_text
        if not data_lines and event_name is None:
            return None
        data_str = "\n".join(data_lines).strip()
        ev = event_name
        event_name = None
        data_lines = []
        if data_str == "" and ev is None:
            return None
        if data_str == "[DONE]":
            return UpstreamDelta(done=True, finish_reason="stop", raw="[DONE]")
        # try JSON
        obj: Any = data_str
        try:
            obj = json.loads(data_str)
        except Exception:
            # plain text payload
            if data_str:
                delta = data_str
                if last_full_text and delta.startswith(last_full_text):
                    delta = delta[len(last_full_text):]
                    last_full_text = data_str
                else:
                    last_full_text += delta
                return UpstreamDelta(text=delta, raw=data_str)
            return None

        text, finish, err = _extract_text_from_obj(obj)
        # handle cumulative content (some APIs send full text each time)
        if text:
            if last_full_text and text.startswith(last_full_text):
                delta_text = text[len(last_full_text):]
                last_full_text = text
            elif last_full_text and last_full_text.startswith(text):
                delta_text = ""
            else:
                # heuristic: if much longer than previous and contains previous, treat as cumulative
                if last_full_text and len(text) > len(last_full_text) and text[: len(last_full_text)] == last_full_text:
                    delta_text = text[len(last_full_text):]
                    last_full_text = text
                else:
                    delta_text = text
                    last_full_text += text
        else:
            delta_text = ""
        done = bool(finish in ("stop", "length", "error") or obj.get("done") is True)
        if ev in ("done", "end", "complete") and not finish:
            finish = "stop"
            done = True
        if ev in ("error",) and not err:
            err = str(obj)
        return UpstreamDelta(text=delta_text, finish_reason=finish, error=err, raw=obj, done=done)

    for line in lines:
        if line is None:
            continue
        # strip trailing CR
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            ev = flush()
            if ev is not None:
                yield ev
                if ev.done:
                    return
            continue
        if line.startswith(":"):
            continue  # comment / keepalive
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
            continue
        # non-SSE line, try as JSON/text chunk
        data_lines.append(line)

    ev = flush()
    if ev is not None:
        yield ev


def parse_json_completion(obj: Any) -> UpstreamDelta:
    text, finish, err = _extract_text_from_obj(obj)
    return UpstreamDelta(text=text or "", finish_reason=finish or ("stop" if not err else "error"), error=err, raw=obj, done=True)
