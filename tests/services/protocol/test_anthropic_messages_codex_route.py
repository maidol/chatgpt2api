import pytest

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import anthropic_v1_messages as messages

READ_TOOL = {"name": "Read", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}
CALL_ITEM = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Read",
             "arguments": "{\"file_path\": \"a.py\"}", "status": "completed"}
UPSTREAM = [
    {"type": "response.created", "response": {"id": "resp_1"}},
    {"type": "response.output_item.done", "output_index": 0, "item": CALL_ITEM},
    {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": [],
                                                "usage": {"input_tokens": 10, "output_tokens": 5}}},
]


class TextPathUsed(BaseException):
    """BaseException so no `except Exception` on the path can swallow it."""


def _forbid_text_path(monkeypatch):
    def forbidden():
        raise TextPathUsed("tool request fell through to the ChatGPT web text path")

    monkeypatch.setattr(messages, "text_backend", forbidden)


def _install_codex(monkeypatch, upstream=UPSTREAM):
    calls = {"tokens": [], "payloads": []}

    def fake_get_text_access_token(**kwargs):
        calls["tokens"].append(kwargs)
        return "tok-codex" if kwargs.get("source_type") == "codex" else ""

    def fake_iter(self, payload):
        calls["payloads"].append(payload)
        yield from [dict(event) for event in upstream]

    monkeypatch.setattr(account_service, "get_text_access_token", fake_get_text_access_token)
    monkeypatch.setattr(account_service, "get_account", lambda token: {"email": "codex@example.com", "source_type": "codex"})
    monkeypatch.setattr(OpenAIBackendAPI, "iter_codex_responses_events", fake_iter, raising=False)
    monkeypatch.setattr(OpenAIBackendAPI, "close", lambda self: None, raising=False)
    return calls


def _body(**extra):
    return {"model": "gpt-5.6", "system": "You are Claude Code.", "tools": [READ_TOOL],
            "messages": [{"role": "user", "content": "read a.py"}], **extra}


def test_tool_request_with_codex_account_returns_tool_use(monkeypatch):
    _forbid_text_path(monkeypatch)
    calls = _install_codex(monkeypatch)

    result = messages.handle(_body())

    assert result["stop_reason"] == "tool_use"
    assert result["content"] == [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "a.py"}}]
    assert result["_account_email"] == "codex@example.com"
    assert calls["tokens"] == [{"source_type": "codex"}]
    sent = calls["payloads"][0]
    assert sent["store"] is False and sent["stream"] is True
    assert sent["tools"][0]["name"] == "Read" and sent["tools"][0]["type"] == "function"
    assert sent["input"][0] == {"type": "message", "role": "developer",
                                "content": [{"type": "input_text", "text": "You are Claude Code."}]}
    assert "Tool output adapter" not in str(sent)


def test_streaming_tool_request_consumes_upstream_before_returning(monkeypatch):
    _forbid_text_path(monkeypatch)
    calls = _install_codex(monkeypatch)

    result = messages.handle(_body(stream=True))

    assert len(calls["payloads"]) == 1
    events = list(result)
    assert events[0]["type"] == "message_start"
    assert events[1]["content_block"]["type"] == "tool_use"
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    assert events[-1]["type"] == "message_stop"


def test_upstream_failure_raises_instead_of_empty_answer(monkeypatch):
    _forbid_text_path(monkeypatch)
    _install_codex(monkeypatch, [{"type": "response.failed",
                                  "response": {"error": {"message": "model not supported"}}}])

    with pytest.raises(RuntimeError, match="model not supported"):
        messages.handle(_body())


def test_without_codex_account_falls_back_to_existing_path(monkeypatch):
    used = []

    def fake_text_backend():
        used.append(True)
        return object()

    def fake_stream(backend, msgs, model):
        yield {"choices": [{"delta": {"content": "no tools here"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(account_service, "get_text_access_token", lambda **kwargs: "")
    monkeypatch.setattr(messages, "text_backend", fake_text_backend)
    monkeypatch.setattr(messages, "stream_text_chat_completion", fake_stream)

    result = messages.handle(_body())

    assert used == [True]
    assert result["content"] == [{"type": "text", "text": "no tools here"}]


def test_request_without_tools_never_asks_for_codex_account(monkeypatch):
    token_calls = []

    def fake_get_text_access_token(**kwargs):
        token_calls.append(kwargs)
        return ""

    def fake_stream(backend, msgs, model):
        yield {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(account_service, "get_text_access_token", fake_get_text_access_token)
    monkeypatch.setattr(messages, "text_backend", lambda: object())
    monkeypatch.setattr(messages, "stream_text_chat_completion", fake_stream)

    messages.handle({"model": "gpt-5.6", "messages": [{"role": "user", "content": "hi"}]})

    assert {"source_type": "codex"} not in token_calls


def test_truncated_response_keeps_done_items_and_reports_max_tokens(monkeypatch):
    _forbid_text_path(monkeypatch)
    text_item = {"type": "message", "content": [{"type": "output_text", "text": "partial answer"}]}
    _install_codex(monkeypatch, [
        {"type": "response.output_item.done", "output_index": 0, "item": text_item},
        {"type": "response.incomplete", "response": {"id": "resp_2", "status": "incomplete", "output": [],
                                                     "incomplete_details": {"reason": "max_output_tokens"}}},
    ])

    result = messages.handle(_body())

    assert result["content"] == [{"type": "text", "text": "partial answer"}]
    assert result["stop_reason"] == "max_tokens"
