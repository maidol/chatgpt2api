from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

from services.openai_backend_api import CODEX_RESPONSES_MODEL
from services.protocol.codex_tool_passthrough import codex_tool_response_events

# 请求/响应的映射规则照抄 sub2api internal/pkg/apicompat 的 anthropic_to_responses.go、
# responses_to_anthropic.go（生产在用）；只保留 Claude Code 一类客户端会用到的部分。
BILLING_HEADER_PREFIX = "x-anthropic-billing-header: "


def has_client_tools(body: dict[str, Any]) -> bool:
    """请求里带了要客户端执行的工具（有名字、不是 web_search 这类服务端工具）。"""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict) and tool.get("name") and not str(tool.get("type") or "").startswith("web_search")
        for tool in tools
    )


def _codex_model(model: object) -> str:
    model = str(model or "").strip()
    return model if model.lower().startswith("gpt-") else CODEX_RESPONSES_MODEL


def _image_url(source: object) -> str:
    if not isinstance(source, dict):
        return ""
    if source.get("type") == "url" and source.get("url"):
        return str(source["url"])
    data = str(source.get("data") or "")
    if not data:
        return ""
    return f"data:{source.get('media_type') or 'image/png'};base64,{data}"


def _system_parts(system: object) -> list[dict[str, str]]:
    if isinstance(system, str):
        texts = [system]
    elif isinstance(system, list):
        texts = [str(block.get("text") or "") for block in system if isinstance(block, dict) and block.get("type") == "text"]
    else:
        texts = []
    return [{"type": "input_text", "text": text} for text in texts if text and not text.startswith(BILLING_HEADER_PREFIX)]


def _tool_result_output(block: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    content = block.get("content")
    if isinstance(content, str):
        return content or "(empty)", []
    texts: list[str] = []
    images: list[dict[str, str]] = []
    for inner in content if isinstance(content, list) else []:
        if not isinstance(inner, dict):
            continue
        if inner.get("type") == "text" and inner.get("text"):
            texts.append(str(inner["text"]))
        elif inner.get("type") == "image" and (url := _image_url(inner.get("source"))):
            images.append({"type": "input_image", "image_url": url})
    return "\n\n".join(texts) or "(empty)", images


def _user_items(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": content}]}]
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    items: list[dict[str, Any]] = []
    tool_images: list[dict[str, str]] = []
    # tool_result 先出，紧跟在上一轮的 function_call 后面；Responses 的 output 只收字符串，图片挪到后面的 user 消息里。
    for block in blocks:
        if block.get("type") == "tool_result":
            output, images = _tool_result_output(block)
            items.append({"type": "function_call_output", "call_id": str(block.get("tool_use_id") or ""), "output": output})
            tool_images.extend(images)
    parts: list[dict[str, str]] = []
    for block in blocks:
        if block.get("type") == "text" and block.get("text"):
            parts.append({"type": "input_text", "text": str(block["text"])})
        elif block.get("type") == "image" and (url := _image_url(block.get("source"))):
            parts.append({"type": "input_image", "image_url": url})
    parts.extend(tool_images)
    if parts:
        items.append({"type": "message", "role": "user", "content": parts})
    return items


def _assistant_items(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}]
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    items: list[dict[str, Any]] = []
    text = "\n\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text" and block.get("text"))
    if text:
        items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    for block in blocks:
        if block.get("type") == "tool_use":
            # call_id 原样用 tool_use 的 id：客户端回 tool_result 时带的就是它。
            items.append({
                "type": "function_call",
                "call_id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
            })
    return items


def _tool_parameters(schema: object) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    if schema.get("type") == "object" and "properties" not in schema:
        return {**schema, "properties": {}}
    return schema


def _tools(tools: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        if str(tool.get("type") or "").startswith("web_search"):
            result.append({"type": "web_search"})
            continue
        result.append({
            "type": "function",
            "name": str(tool["name"]),
            "description": str(tool.get("description") or ""),
            "parameters": _tool_parameters(tool.get("input_schema")),
            "strict": False,
        })
    return result


def _tool_choice(choice: object) -> object:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "name": str(choice["name"])}
    return None


def anthropic_to_codex_body(body: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages 请求 → Responses 请求体（再由 codex_request_body 补齐 codex 契约）。"""
    items: list[dict[str, Any]] = []
    system = _system_parts(body.get("system"))
    if system:
        items.append({"type": "message", "role": "developer", "content": system})
    for message in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            items.extend(_assistant_items(message.get("content")))
        else:
            items.extend(_user_items(message.get("content")))
    payload: dict[str, Any] = {
        "model": _codex_model(body.get("model")),
        "input": items,
        "tools": _tools(body.get("tools")),
        "parallel_tool_calls": True,
    }
    choice = _tool_choice(body.get("tool_choice"))
    if choice is not None:
        payload["tool_choice"] = choice
    if isinstance(body.get("tool_choice"), dict) and body["tool_choice"].get("disable_parallel_tool_use"):
        payload["parallel_tool_calls"] = False
    return payload


def _tool_input(name: str, arguments: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(arguments or "{}"))
    except Exception:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    # GPT 给 Claude Code 的 Read 工具填 pages:"" 时 Read 会报错，sub2api 同样删掉它。
    if name == "Read" and parsed.get("pages") == "":
        parsed.pop("pages")
    return parsed


def codex_response_to_message(response: dict[str, Any], model: str) -> dict[str, Any]:
    """codex 的最终 response → Anthropic 非流式消息。reasoning 项不转（不回放思考链）。"""
    content: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                    content.append({"type": "text", "text": str(part["text"])})
        elif item.get("type") == "function_call":
            name = str(item.get("name") or "")
            content.append({
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex}"),
                "name": name,
                "input": _tool_input(name, item.get("arguments")),
            })
    if not content:
        content.append({"type": "text", "text": ""})
    details = response.get("incomplete_details") if isinstance(response.get("incomplete_details"), dict) else {}
    if response.get("status") == "incomplete" and details.get("reason") == "max_output_tokens":
        stop_reason = "max_tokens"
    elif any(block["type"] == "tool_use" for block in content):
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return {
        "id": f"msg_{response.get('id') or uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_input_tokens": cached,
        },
    }


def message_stream_events(message: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """把整条消息按 Anthropic SSE 事件顺序吐出去。上游 codex 通道本来就是读完整条流才返回（R1），这里不假装逐字流。"""
    usage = message["usage"]
    start = {key: value for key, value in message.items() if key not in {"content", "stop_reason", "usage"}}
    yield {"type": "message_start", "message": {
        **start, "content": [], "stop_reason": None,
        "usage": {**usage, "output_tokens": 0},
    }}
    for index, block in enumerate(message["content"]):
        if block["type"] == "tool_use":
            yield {"type": "content_block_start", "index": index,
                   "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}
            yield {"type": "content_block_delta", "index": index,
                   "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}}
        else:
            yield {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}
            yield {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}}
        yield {"type": "content_block_stop", "index": index}
    yield {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
           "usage": {"output_tokens": usage["output_tokens"]}}
    yield {"type": "message_stop"}


def codex_message_result(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]] | None:
    """有 codex 号就走原生工具调用，返回 Anthropic 消息（stream=true 时是事件迭代器）；没有返回 None。"""
    events = codex_tool_response_events(anthropic_to_codex_body(body))
    if events is None:
        return None
    response: dict[str, Any] | None = None
    account_email = ""
    done_items: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type in {"error", "response.failed"}:
            failed = event.get("response") if isinstance(event.get("response"), dict) else event
            error = failed.get("error") if isinstance(failed.get("error"), dict) else {}
            raise RuntimeError(f"codex responses failed: {error.get('message') or event_type}")
        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            done_items.append(event["item"])
        if event_type in {"response.completed", "response.incomplete"} and isinstance(event.get("response"), dict):
            response = event["response"]
            account_email = str(event.get("_account_email") or account_email)
    if response is None:
        raise RuntimeError("codex responses stream ended without response.completed")
    if not response.get("output") and done_items:
        response = {**response, "output": done_items}
    message = codex_response_to_message(response, str(body.get("model") or CODEX_RESPONSES_MODEL))
    if body.get("stream"):
        events_out = list(message_stream_events(message))
        if account_email:
            events_out[-1]["_account_email"] = account_email
        return iter(events_out)
    if account_email:
        message["_account_email"] = account_email
    return message
