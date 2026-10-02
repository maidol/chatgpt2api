import copy

from services.openai_backend_api import CODEX_RESPONSES_MODEL
from services.protocol import codex_tool_passthrough as passthrough

FUNCTION_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}


def test_codex_request_body_enforces_codex_contract():
    body = {
        "model": "auto",
        "input": "hello",
        "tools": [FUNCTION_TOOL],
        "tool_choice": None,
        "stream": False,
        "store": True,
        "temperature": 0.2,
        "max_output_tokens": 100,
        "metadata": {"a": "b"},
        "_call_id": "abc",
    }
    original = copy.deepcopy(body)

    payload = passthrough.codex_request_body(body)

    assert body == original
    assert payload["model"] == CODEX_RESPONSES_MODEL
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == passthrough.CODEX_DEFAULT_INSTRUCTIONS
    assert payload["input"] == [{"type": "message", "role": "user", "content": "hello"}]
    assert payload["tools"] == [FUNCTION_TOOL]
    for key in ("tool_choice", "temperature", "max_output_tokens", "metadata", "_call_id"):
        assert key not in payload


def test_codex_request_body_keeps_client_model_instructions_and_tool_history():
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "read a.py"}]},
        {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "print(1)"},
    ]
    body = {
        "model": "gpt-5.4",
        "instructions": "You are Codex.",
        "input": history,
        "tools": [FUNCTION_TOOL],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    payload = passthrough.codex_request_body(body)

    assert payload["model"] == "gpt-5.4"
    assert payload["instructions"] == "You are Codex."
    assert payload["input"] == history
    assert payload["tool_choice"] == "auto"
    assert payload["parallel_tool_calls"] is True


class FakeCodexBackend:
    def __init__(self, events):
        self.events = events
        self.payload = None
        self.closed = False

    def iter_codex_responses_events(self, payload):
        self.payload = payload
        yield from self.events

    def close(self):
        self.closed = True


def test_stream_fills_empty_completed_output_from_done_items():
    call_item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file",
                 "arguments": "{\"path\": \"a.py\"}", "status": "completed"}
    backend = FakeCodexBackend([
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.done", "output_index": 0, "item": call_item},
        {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
    ])

    events = list(passthrough.stream_codex_tool_response(backend, {"model": "gpt-5.5"}, "codex@example.com"))

    completed = events[-1]
    assert completed["type"] == "response.completed"
    assert completed["response"]["output"] == [call_item]
    assert completed["_account_email"] == "codex@example.com"
    assert completed["response"]["_account_email"] == "codex@example.com"
    assert backend.payload == {"model": "gpt-5.5"}
    assert backend.closed


def test_stream_keeps_non_empty_completed_output():
    upstream_output = [{"type": "message", "id": "msg_1", "content": []}]
    backend = FakeCodexBackend([
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "x", "arguments": "{}"}},
        {"type": "response.completed", "response": {"id": "resp_2", "output": upstream_output}},
    ])

    events = list(passthrough.stream_codex_tool_response(backend, {}, ""))

    assert events[-1]["response"]["output"] == upstream_output
    assert "_account_email" not in events[-1]


def test_no_codex_account_returns_none(monkeypatch):
    calls = []

    def fake_get_text_access_token(**kwargs):
        calls.append(kwargs)
        return ""

    monkeypatch.setattr(passthrough.account_service, "get_text_access_token", fake_get_text_access_token)

    assert passthrough.codex_tool_response_events({"input": "hi", "tools": [FUNCTION_TOOL]}) is None
    assert calls == [{"source_type": "codex"}]
