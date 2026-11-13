"""工具调用协议层。

上游 turns API 不暴露 tools 字段，因此工具调用在代理层实现：
1. 把工具定义注入 system 级协议提示，要求模型用 <tool_calls>[...]</tool_calls> 块回复；
2. 解析模型输出，转成 OpenAI tool_calls / Anthropic tool_use 结构；
3. tool / tool_result 历史序列化回纯文本对话。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

OPEN = "<tool_calls>"
CLOSE = "</tool_calls>"
MAX_TOOL_RESULT_CHARS = 24000
MAX_TOOL_DESC_CHARS = 300


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"...[truncated {len(s) - n} chars]"


def strip_think(text: str) -> str:
    """剥离 <think>...</think> 推理块（借鉴 llm-tool-capability）。"""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _repair_json(s: str) -> str:
    """轻度 JSON 修复：去掉围栏残留、行注释、块注释、尾逗号（借鉴 llm-tool-capability）。"""
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"^\s*//.*$", "", s, flags=re.M)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _safe_json(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return {}


def compact_schema(schema: Any, max_desc: int = 160, depth: int = 0) -> Any:
    """压缩 JSON schema，避免几十个工具把提示撑爆。"""
    if depth > 6:
        return schema
    if isinstance(schema, dict):
        out: Dict[str, Any] = {}
        for k, v in schema.items():
            if k == "description" and isinstance(v, str):
                out[k] = _truncate(v, max_desc)
            elif k in ("$schema", "title", "examples", "$id", "$defs"):
                continue
            elif isinstance(v, (dict, list)):
                out[k] = compact_schema(v, max_desc, depth + 1)
            else:
                out[k] = v
        return out
    if isinstance(schema, list):
        return [compact_schema(x, max_desc, depth + 1) for x in schema]
    return schema


def normalize_tools(tools: Optional[List[Dict[str, Any]]],
                    compact: bool = True) -> List[Dict[str, Any]]:
    """OpenAI / Anthropic 两种工具定义统一成 {name, description, parameters}。"""
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
        out.append({
            "name": str(name),
            "description": _truncate(str(fn.get("description") or ""), MAX_TOOL_DESC_CHARS),
            "parameters": compact_schema(params) if compact else params,
        })
    return out


def tool_protocol_block(tools_norm: List[Dict[str, Any]],
                        tool_choice: Any = None) -> Optional[str]:
    """XML 块协议（降级备选）。首选是 JSON 合同（见 json_contract_block）。"""
    if not tools_norm:
        return None
    mode, forced = "auto", None
    if isinstance(tool_choice, str):
        mode = tool_choice
    elif isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            forced = (tool_choice.get("function") or {}).get("name")
            mode = "forced" if forced else "auto"
        elif tool_choice.get("type") == "tool":
            forced = tool_choice.get("name")
            mode = "forced" if forced else "auto"
    if mode == "none":
        return None

    lines = [
        "[TOOL USE PROTOCOL]",
        "You can invoke tools. Available tools (JSON):",
        "<tools>" + json.dumps(tools_norm, ensure_ascii=False) + "</tools>",
        "",
        "To invoke tool(s), reply with EXACTLY one block and nothing else:",
        '<tool_calls>[{"name":"tool_name","arguments":{...}}]</tool_calls>',
        "Rules:",
        '- "arguments" must be a JSON object conforming to the tool\'s parameters schema.',
        "- Put multiple call objects in the array to invoke several tools at once.",
        "- The block must contain valid JSON only; no commentary inside or around it.",
        "- Tool results arrive later as messages like '[Tool result (call_id=...)]: ...';"
        " use them, then either call more tools or answer the user in plain text.",
        "- If no tool is needed, answer in plain text and never emit the block.",
    ]
    if mode == "required":
        lines.append("You MUST invoke at least one tool now.")
    if forced:
        lines.append(f'You MUST invoke the tool "{forced}" now.')
    return "\n".join(lines)


def json_contract_block(tools_norm: List[Dict[str, Any]],
                        tool_choice: Any = None) -> Optional[str]:
    """JSON 合同提示：配合上游 response_format=json_object 使用（实测可靠）。

    模型每轮只输出一个 JSON 对象：
        {"type":"tool_calls","calls":[{"name":"...","arguments":{...}}]}
        {"type":"final","content":"..."}
    """
    if not tools_norm:
        return None
    mode, forced = "auto", None
    if isinstance(tool_choice, str):
        mode = tool_choice
    elif isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            forced = (tool_choice.get("function") or {}).get("name")
            mode = "forced" if forced else "auto"
        elif tool_choice.get("type") == "tool":
            forced = tool_choice.get("name")
            mode = "forced" if forced else "auto"
    if mode == "none":
        return None

    # 扁平的函数清单（比完整 schema 省很多 token，且实测模型能正确给参）
    fn_lines = []
    for i, t in enumerate(tools_norm, 1):
        params = t.get("parameters") or {}
        req = params.get("required") or []
        props = params.get("properties") or {}
        sig = ", ".join(
            f"{k}: {json.dumps(v.get('type', 'any'))}{'' if k not in req else ', required'}"
            for k, v in props.items()
        ) or "no arguments"
        desc = t.get("description") or ""
        fn_lines.append(f"{i}. {t['name']}({sig}) - {desc}")

    lines = [
        "You are an AI assistant with access to function tools through an integration layer.",
        "Tool calls you output are executed by the client application, and their results are sent back to you in the next message as lines like '[Tool result (call_id=...)]: ...'.",
        "",
        "Respond with exactly one JSON object on every turn:",
        '- to call tools: {"type":"tool_calls","calls":[{"name":"...","arguments":{...}}]}',
        '- to answer the user (after tool results arrive, or when no tool is needed): {"type":"final","content":"..."}',
        "",
        "Example turn:",
        'User: What is the weather in Paris?',
        'Assistant: {"type":"tool_calls","calls":[{"name":"get_weather","arguments":{"city":"Paris"}}]}',
        '[Tool result (call_id=call_1)]: 18C, clear sky',
        'Assistant: {"type":"final","content":"It is 18C and clear in Paris."}',
        "",
        "Available functions:",
        *fn_lines,
        "",
        "Notes:",
        "- arguments must satisfy the function signature above.",
        "- Several call objects may be listed to invoke multiple functions.",
        "- Never wrap the JSON in markdown fences; never write anything outside the JSON object.",
    ]
    if mode == "required":
        lines.append('You must use {"type":"tool_calls", ...} on this turn.')
    if forced:
        lines.append(f'You must call the function "{forced}" on this turn.')
    return "\n".join(lines)


# ---------- 消息归一化 ----------

def _norm_tool_call_openai(tc: Dict[str, Any]) -> Dict[str, Any]:
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    args_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
    return {"id": tc.get("id") or "call_" + uuid.uuid4().hex[:12],
            "name": str(fn.get("name") or ""), "arguments": args_str}


def normalize_openai_messages(messages: List[Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """ChatMessage 列表 -> (system_text, normalized msgs)。"""
    system_parts: List[str] = []
    msgs: List[Dict[str, Any]] = []
    for m in messages:
        role = (getattr(m, "role", "") or "").lower()
        text = m.text_content() if hasattr(m, "text_content") else ""
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
            continue
        entry: Dict[str, Any] = {"role": role, "text": text, "tool_calls": [], "tool_results": []}
        tc = getattr(m, "tool_calls", None)
        if tc:
            if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
                entry["tool_calls"].append(_norm_tool_call_openai(tc))
            elif isinstance(tc, list):
                entry["tool_calls"].extend(_norm_tool_call_openai(x) for x in tc if isinstance(x, dict))
        if role == "tool":
            entry["tool_results"].append({
                "id": getattr(m, "tool_call_id", None) or "",
                "content": _truncate(text, MAX_TOOL_RESULT_CHARS),
            })
            entry["text"] = ""
        msgs.append(entry)
    return "\n\n".join(system_parts), msgs


def extract_system_anthropic(body: Dict[str, Any]) -> str:
    s = body.get("system")
    if not s:
        return ""
    if isinstance(s, str):
        return s
    if isinstance(s, list):
        parts = []
        for b in s:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
        return "\n\n".join(parts)
    return str(s)


def normalize_anthropic_messages(raw_msgs: List[Any]) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    for m in raw_msgs or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user").lower()
        entry: Dict[str, Any] = {"role": role, "text": "", "tool_calls": [], "tool_results": []}
        c = m.get("content")
        if isinstance(c, str):
            entry["text"] = c
        elif isinstance(c, list):
            texts: List[str] = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    texts.append(str(b.get("text") or ""))
                elif t == "tool_use":
                    entry["tool_calls"].append({
                        "id": str(b.get("id") or "call_" + uuid.uuid4().hex[:12]),
                        "name": str(b.get("name") or ""),
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    })
                elif t == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        txt = "\n".join(
                            str(x.get("text") or "") for x in content
                            if isinstance(x, dict) and x.get("type") == "text"
                        ) or "[non-text tool result]"
                    else:
                        txt = str(content or "")
                    entry["tool_results"].append({
                        "id": str(b.get("tool_use_id") or ""),
                        "content": _truncate(txt, MAX_TOOL_RESULT_CHARS),
                    })
                elif t == "image":
                    texts.append("[image]")
            entry["text"] = "\n".join(x for x in texts if x)
        msgs.append(entry)
    return msgs


# ---------- 序列化 ----------

def serialize_conversation(msgs: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for m in msgs:
        r = m.get("role")
        if r == "user":
            if m.get("text"):
                lines.append("User: " + m["text"])
            for tr in m.get("tool_results") or []:
                lines.append(f'[Tool result (call_id={tr["id"]})]:\n{tr["content"]}')
        elif r == "assistant":
            if m.get("text"):
                lines.append("Assistant: " + m["text"])
            for tc in m.get("tool_calls") or []:
                lines.append(f'Assistant tool call: {tc["name"]}({tc["arguments"]}) [call_id={tc["id"]}]')
        elif r == "tool":
            for tr in m.get("tool_results") or []:
                lines.append(f'[Tool result (call_id={tr["id"]})]:\n{tr["content"]}')
    return "\n\n".join(lines)


def build_prompt(system_text: str, msgs: List[Dict[str, Any]],
                 tools: Optional[List[Any]] = None, tool_choice: Any = None,
                 compact: bool = True, style: str = "json") -> str:
    parts: List[str] = []
    if system_text:
        parts.append(system_text)
    if tools:
        norm = normalize_tools(tools, compact=compact)
        proto = (json_contract_block(norm, tool_choice) if style == "json"
                 else tool_protocol_block(norm, tool_choice))
        if proto:
            parts.append(proto)
    parts.append(serialize_conversation(msgs))
    return "\n\n".join(p for p in parts if p.strip())


def parse_fenced_calls(text: str) -> Tuple[List[Dict[str, Any]], str, bool]:
    """解析 ```tool_call / ```json 围栏块（借鉴 llm-tool-capability 的 lenient fences）。

    支持多个连续块或单块内数组。返回 (calls, clean_text, found_any)。
    """
    calls: List[Dict[str, Any]] = []
    pattern = re.compile(r"```[a-zA-Z_]*\s*\n(.*?)```", re.S)
    leftover_parts: List[str] = []
    last_end = 0
    found = False
    for m in pattern.finditer(text):
        leftover_parts.append(text[last_end:m.start()])
        last_end = m.end()
        inner = m.group(1).strip()
        label = text[m.start():m.start() + 12].lower()
        if not (inner.startswith("{") or inner.startswith("[")):
            continue
        if "tool_call" not in label and not (inner.lstrip().startswith(("{", "["))):
            continue
        try:
            obj = json.loads(_repair_json(inner))
        except Exception:
            continue
        items = obj if isinstance(obj, list) else [obj]
        ok = True
        items2, ok = _normalize_call_items(items)
        if items2 and ok:
            calls.extend(items2)
            found = True
    leftover_parts.append(text[last_end:])
    clean = "\n".join(p for p in leftover_parts if p.strip()).strip()
    return calls, clean, found


def parse_model_output(text: str) -> Tuple[List[Dict[str, Any]], str, bool]:
    """统一解析入口：JSON 合同 → <tool_calls> → 围栏块 → 裸 JSON。

    返回 (calls, clean_text, retryable)。
    """
    t = strip_think(text)
    # 1) JSON 合同
    parsed = parse_json_contract(t)
    if parsed is not None:
        return parsed["calls"], parsed["text"], parsed["retryable"]
    # 2) <tool_calls> 块
    calls, clean, retryable = parse_tool_calls(t)
    if calls or retryable:
        return calls, clean, retryable
    # 3) 围栏块
    calls, clean, found = parse_fenced_calls(t)
    if calls:
        return calls, clean, False
    return [], t, False


def parse_json_contract(text: str) -> Optional[Dict[str, Any]]:
    """解析 JSON 合同输出。

    返回 {"calls": [...], "text": str, "retryable": bool}；
    输出根本不是 JSON 合同时返回 None。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    t = str(obj.get("type") or "").lower()
    items = obj.get("calls") or obj.get("tool_calls") or obj.get("tools")
    if t in ("tool_calls", "tool_use", "tools", "function_call") or (not t and items):
        calls, ok = _normalize_call_items(items if isinstance(items, list) else [])
        return {"calls": calls, "text": str(obj.get("content") or ""),
                "retryable": not (ok and calls)}
    if t in ("final", "answer", "message", "response", "text") or "content" in obj:
        content = obj.get("content") or obj.get("answer") or obj.get("text") or ""
        return {"calls": [], "text": str(content), "retryable": False}
    # 未知结构但确实是 JSON 对象：能救则救
    calls, ok = _normalize_call_items(items if isinstance(items, list) else [])
    if calls:
        return {"calls": calls, "text": str(obj.get("content") or ""), "retryable": False}
    return {"calls": [], "text": s, "retryable": False}


# ---------- 输出解析 ----------

def _normalize_call_items(items: List[Any]) -> Tuple[List[Dict[str, Any]], bool]:
    calls: List[Dict[str, Any]] = []
    ok = True
    for it in items:
        if not isinstance(it, dict):
            ok = False
            continue
        name = it.get("name") or it.get("tool") or it.get("function")
        if isinstance(name, dict):
            name = name.get("name")
        if not name:
            ok = False
            continue
        args = it.get("arguments", it.get("input", it.get("parameters", {})))
        if isinstance(args, str):
            args_str = args.strip() or "{}"
        else:
            args_str = json.dumps(args or {}, ensure_ascii=False)
        calls.append({
            "id": str(it.get("id") or "call_" + uuid.uuid4().hex[:12]),
            "name": str(name),
            "arguments": args_str,
            "arguments_obj": _safe_json(args_str),
        })
    return calls, ok


def parse_tool_calls(text: str) -> Tuple[List[Dict[str, Any]], str, bool]:
    """返回 (calls, clean_text, retryable)。

    retryable=True 表示模型给了 <tool_calls> 块但内容不是合法 JSON，值得重试一次。
    """
    idx = text.rfind(OPEN)
    if idx < 0:
        # 无标记：尝试裸 JSON 数组兜底（模型有时忘了块标记）
        s = text.strip()
        if (s.startswith("[") or s.startswith("{")) and len(s) < 8000:
            try:
                data = json.loads(s)
                items = data if isinstance(data, list) else [data]
                if items and all(isinstance(x, dict) and (x.get("name") or x.get("tool")) for x in items) \
                        and all(("arguments" in x or "input" in x or "parameters" in x) for x in items):
                    calls, ok = _normalize_call_items(items)
                    if calls and ok:
                        return calls, "", False
            except Exception:
                pass
        return [], text, False

    had_marker = True
    end = text.find(CLOSE, idx)
    inner = text[idx + len(OPEN): end if end >= 0 else len(text)]
    clean = (text[:idx] + (text[end + len(CLOSE):] if end >= 0 else "")).strip()
    inner = inner.strip()
    if inner.startswith("```"):
        inner = re.sub(r"^```[a-zA-Z]*\n?", "", inner)
        inner = re.sub(r"\n?```$", "", inner).strip()
    data = None
    try:
        data = json.loads(inner)
    except Exception:
        m = re.search(r"\[.*\]", inner, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if data is None:
        return [], clean, True
    if isinstance(data, dict):
        items = data.get("calls") or data.get("tool_calls") or (
            [data] if data.get("name") or data.get("tool") else None)
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if not isinstance(items, list) or not items:
        return [], clean, True
    calls, ok = _normalize_call_items(items)
    return calls, clean, had_marker and (not ok or not calls)


def to_openai_tool_calls(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"index": i, "id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}}
            for i, c in enumerate(calls)]


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + other)
