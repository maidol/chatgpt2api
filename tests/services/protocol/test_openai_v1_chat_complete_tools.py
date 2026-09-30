import json

import pytest
from fastapi import HTTPException

from services.protocol import openai_v1_chat_complete as chat
from services.protocol.chat_completion_cache import cache_key


def tool(name="lookup"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Find information",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }


def envelope(name="lookup", arguments=None):
    return json.dumps({
        "__chatgpt2api_tool_call__": True,
        "tool_calls": [{"name": name, "arguments": arguments or {"q": "hello"}}],
    })


class FakeBackend:
    def __init__(self, response):
        self.response = response
        self.account_email = "test@example.com"
        self.messages = None
        self.closed = False

    def collect(self, request):
        self.messages = request.messages
        return self.response

    def close(self):
        self.closed = True


def test_non_streaming_function_tool_returns_openai_tool_calls(monkeypatch):
    backend = FakeBackend(envelope(arguments={"q": '上海 "天气"'}))
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "collect_text", lambda _backend, request: backend.collect(request))

    response = chat.handle({
        "model": "auto",
        "messages": [{"role": "user", "content": "上海天气？"}],
        "tools": [tool()],
    })

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["id"].startswith("call_")
    assert call["type"] == "function"
    assert call["function"]["name"] == "lookup"
    assert json.loads(call["function"]["arguments"]) == {"q": '上海 "天气"'}
    assert response["usage"]["completion_tokens"] > 0
    assert backend.closed
    assert "Available functions" in backend.messages[0]["content"]


def test_non_streaming_function_tool_returns_regular_text_for_unmarked_reply(monkeypatch):
    backend = FakeBackend("Hello from the assistant.")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "collect_text", lambda _backend, request: backend.collect(request))

    response = chat.handle({"messages": [{"role": "user", "content": "Hi"}], "tools": [tool()]})

    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["choices"][0]["message"]["content"] == "Hello from the assistant."
    assert "tool_calls" not in response["choices"][0]["message"]


def test_non_streaming_none_choice_suppresses_tool_instruction(monkeypatch):
    backend = FakeBackend("I will answer without using a function.")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "collect_text", lambda _backend, request: backend.collect(request))

    response = chat.handle({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [tool()],
        "tool_choice": "none",
    })

    assert response["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in response["choices"][0]["message"]
    assert "Tool choice: {\"mode\":\"none\"}" in backend.messages[0]["content"]


def test_non_streaming_invalid_tool_envelope_maps_to_502_without_leak(monkeypatch):
    marker = '{"__chatgpt2api_tool_call__":true,"tool_calls":["bad"]}'
    backend = FakeBackend(marker)
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "collect_text", lambda _backend, request: backend.collect(request))

    with pytest.raises(HTTPException) as error:
        chat.handle({"messages": [{"role": "user", "content": "Hi"}], "tools": [tool()]})

    assert error.value.status_code == 502
    assert marker not in str(error.value.detail)


def test_function_tool_rejects_web_search_mix(monkeypatch):
    monkeypatch.setattr(chat, "text_backend", lambda: pytest.fail("backend must not run"))

    with pytest.raises(HTTPException) as error:
        chat.handle({
            "messages": [{"role": "user", "content": "Search"}],
            "tools": [tool(), {"type": "web_search"}],
        })

    assert error.value.status_code == 400


def test_function_tool_rejects_undeclared_forced_choice(monkeypatch):
    monkeypatch.setattr(chat, "text_backend", lambda: pytest.fail("backend must not run"))

    with pytest.raises(HTTPException) as error:
        chat.handle({
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [tool()],
            "tool_choice": {"type": "function", "function": {"name": "other"}},
        })

    assert error.value.status_code == 400


def test_tool_history_round_trip_resumes_with_tool_result(monkeypatch):
    backend = FakeBackend("The result says it is sunny.")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "collect_text", lambda _backend, request: backend.collect(request))
    messages = [
        {"role": "user", "content": "What is the weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_abc",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"weather"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_abc", "name": "lookup", "content": "Sunny, 26C"},
    ]

    response = chat.handle({"messages": messages})

    assert response["choices"][0]["message"]["content"] == "The result says it is sunny."
    serialized = [message["content"] for message in backend.messages]
    assert any("call_abc" in content for content in serialized)
    result_content = next(content for content in serialized if "Sunny, 26C" in content)
    assert "untrusted" in result_content.lower()


def test_streaming_function_tool_buffers_and_returns_openai_tool_call_chunks(monkeypatch):
    backend = FakeBackend(envelope(arguments={"q": '上海 "天气"'}))
    observed_request = {}

    def fake_stream(_backend, request):
        observed_request["messages"] = request.messages
        yield '{"__chatgpt2api_tool_call__":true,'
        yield '"tool_calls":[{"name":"lookup","arguments":{"q":"上海 \\\"天气\\\""}}]}'

    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "stream_text_deltas", fake_stream)

    chunks = list(chat.handle({
        "messages": [{"role": "user", "content": "上海天气？"}],
        "tools": [tool()],
        "stream": True,
    }))

    choices = [chunk["choices"][0] for chunk in chunks]
    assert choices[0]["delta"] == {"role": "assistant"}
    calls = [call for choice in choices for call in choice["delta"].get("tool_calls", [])]
    assert len(calls) == 1
    assert calls[0]["index"] == 0
    assert calls[0]["id"].startswith("call_")
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "lookup"
    assert json.loads(calls[0]["function"]["arguments"]) == {"q": '上海 "天气"'}
    assert choices[-1]["finish_reason"] == "tool_calls"
    assert all("__chatgpt2api_tool_call__" not in json.dumps(chunk) for chunk in chunks)
    assert backend.closed
    assert "Available functions" in observed_request["messages"][0]["content"]


def test_streaming_function_tool_returns_regular_text_chunks(monkeypatch):
    backend = FakeBackend("unused")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "stream_text_deltas", lambda _backend, _request: iter(["Hello", " world"]))

    chunks = list(chat.handle({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [tool()],
        "stream": True,
    }))

    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks) == "Hello world"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_streaming_invalid_envelope_fails_before_first_chunk_and_does_not_leak(monkeypatch):
    marker = '{"__chatgpt2api_tool_call__":true,"tool_calls":["bad"]}'
    backend = FakeBackend("unused")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "stream_text_deltas", lambda _backend, _request: iter([marker]))
    chunks = chat.handle({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [tool()],
        "stream": True,
    })

    with pytest.raises(HTTPException) as error:
        next(chunks)

    assert error.value.status_code == 502
    assert marker not in str(error.value.detail)


def test_streaming_required_choice_rejects_text_answer_before_yield(monkeypatch):
    backend = FakeBackend("unused")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "stream_text_deltas", lambda _backend, _request: iter(["No tool call." ]))
    chunks = chat.handle({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [tool()],
        "tool_choice": "required",
        "stream": True,
    })
    with pytest.raises(HTTPException) as error:
        next(chunks)
    assert error.value.status_code == 502


def test_cache_keys_separate_tool_schemas_choices_results_and_stream_modes():
    base = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [tool()],
        "tool_choice": "auto",
    }
    normalized = [{"role": "user", "content": "Hi"}]
    base_key = cache_key(base, normalized, stream=False)

    changed_schema = {**base, "tools": [tool("other")]}
    changed_choice = {**base, "tool_choice": "none"}
    changed_result = {
        **base,
        "messages": [
            {"role": "tool", "tool_call_id": "call_x", "content": "different"}
        ],
    }

    keys = {
        base_key,
        cache_key(changed_schema, normalized, stream=False),
        cache_key(changed_choice, normalized, stream=False),
        cache_key(changed_result, changed_result["messages"], stream=False),
        cache_key(base, normalized, stream=True),
    }
    assert len(keys) == 5
    assert cache_key(base, normalized, stream=False) == base_key


def test_function_tool_requests_bypass_response_caches(monkeypatch):
    backend = FakeBackend("Normal text")
    monkeypatch.setattr(chat, "text_backend", lambda: backend)
    monkeypatch.setattr(chat, "collect_text", lambda _backend, request: backend.collect(request))
    monkeypatch.setattr(
        chat.chat_completion_cache,
        "get_or_compute_response",
        lambda *_args, **_kwargs: pytest.fail("tool bridge requests must not cache raw envelope responses"),
    )

    response = chat.handle({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [tool()],
    })

    assert response["choices"][0]["message"]["content"] == "Normal text"


def test_web_search_only_request_keeps_existing_search_route(monkeypatch):
    expected = {"route": "web-search"}
    monkeypatch.setattr(chat, "web_search_chat_response", lambda _messages, _model: expected)

    assert chat.handle({
        "messages": [{"role": "user", "content": "Search for a fact"}],
        "tools": [{"type": "web_search"}],
    }) is expected


def test_image_chat_keeps_existing_image_route(monkeypatch):
    expected = {"route": "image"}
    monkeypatch.setattr(chat, "is_image_chat_request", lambda _body: True)
    monkeypatch.setattr(chat, "image_chat_response", lambda _body: expected)

    assert chat.handle({"messages": [{"role": "user", "content": "draw a cat"}]}) is expected


def test_malformed_tool_history_is_rejected_before_backend(monkeypatch):
    monkeypatch.setattr(chat, "text_backend", lambda: pytest.fail("backend must not run"))

    with pytest.raises(HTTPException) as error:
        chat.handle({"messages": [{"role": "tool", "tool_call_id": "missing", "content": "result"}]})

    assert error.value.status_code == 400
