from __future__ import annotations

import copy
from typing import Any, Iterator

from services.account_service import account_service
from services.openai_backend_api import CODEX_RESPONSES_MODEL, OpenAIBackendAPI
from utils.log import logger

# codex/responses 不接受这些字段，带上会 400（与 sub2api 的 openAICodexOAuthUnsupportedFields 一致）。
CODEX_UNSUPPORTED_FIELDS = (
    "max_output_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "chat_template_kwargs",
    "user",
    "metadata",
    "prompt_cache_retention",
    "safety_identifier",
    "stream_options",
    "truncation",
    "stop_sequences",
)
CODEX_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


def codex_request_body(body: dict[str, Any]) -> dict[str, Any]:
    """把客户端的 Responses 请求体整理成 codex/responses 接受的形状，工具与历史原样保留。"""
    payload = {
        key: copy.deepcopy(value)
        for key, value in body.items()
        if value is not None and not str(key).startswith("_")
    }
    for key in CODEX_UNSUPPORTED_FIELDS:
        payload.pop(key, None)
    model = str(payload.get("model") or "").strip()
    payload["model"] = CODEX_RESPONSES_MODEL if model in {"", "auto"} else model
    payload["store"] = False
    payload["stream"] = True
    if not str(payload.get("instructions") or "").strip():
        payload["instructions"] = CODEX_DEFAULT_INSTRUCTIONS
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        payload["input"] = [{"type": "message", "role": "user", "content": raw_input}] if raw_input.strip() else []
    elif raw_input is None:
        payload["input"] = []
    return payload


def stream_codex_tool_response(backend, payload: dict[str, Any], account_email: str) -> Iterator[dict[str, Any]]:
    """透传上游事件；上游 response.completed 的 output 为空时，用 output_item.done 的 item 补齐。"""
    done_items: list[dict[str, Any]] = []
    try:
        for event in backend.iter_codex_responses_events(payload):
            event_type = str(event.get("type") or "")
            if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
                done_items.append(event["item"])
            if event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    if not response.get("output") and done_items:
                        response["output"] = list(done_items)
                    if account_email:
                        response["_account_email"] = account_email
                if account_email:
                    event["_account_email"] = account_email
            yield event
    finally:
        backend.close()


def codex_tool_response_events(body: dict[str, Any]) -> Iterator[dict[str, Any]] | None:
    """有 codex 号就走原生工具调用；没有返回 None，由调用方沿用原来的文本路径。"""
    access_token = account_service.get_text_access_token(source_type="codex")
    if not access_token:
        logger.warning({"event": "codex_tool_passthrough_no_account"})
        return None
    account = account_service.get_account(access_token) or {}
    backend = OpenAIBackendAPI(access_token=access_token)
    return stream_codex_tool_response(backend, codex_request_body(body), str(account.get("email") or "").strip())
