import uuid

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import openai_v1_response as responses

FUNCTION_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
CALL_ITEM = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file",
             "arguments": "{\"path\": \"a.py\"}", "status": "completed"}


class TextPathUsed(BaseException):
    """BaseException so no `except Exception` on the path can swallow it."""


def _forbid_text_path(monkeypatch):
    def forbidden_text_backend():
        raise TextPathUsed("tool request fell through to the ChatGPT web text path")

    monkeypatch.setattr(responses, "text_backend", forbidden_text_backend)


def _install_codex_account(monkeypatch, token_calls):
    def fake_get_text_access_token(**kwargs):
        token_calls.append(kwargs)
        return "tok-codex" if kwargs.get("source_type") == "codex" else ""

    monkeypatch.setattr(account_service, "get_text_access_token", fake_get_text_access_token)
    monkeypatch.setattr(account_service, "get_account", lambda token: {"email": "codex@example.com", "source_type": "codex"})


def _install_upstream(monkeypatch, sent_payloads):
    def fake_iter_codex_responses_events(self, payload):
        sent_payloads.append(payload)
        yield {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}
        yield {"type": "response.output_item.added", "output_index": 0, "item": dict(CALL_ITEM, status="in_progress")}
        yield {"type": "response.output_item.done", "output_index": 0, "item": CALL_ITEM}
        yield {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": []}}

    monkeypatch.setattr(OpenAIBackendAPI, "iter_codex_responses_events", fake_iter_codex_responses_events, raising=False)


def test_function_tool_request_uses_codex_native_path(monkeypatch):
    token_calls, sent = [], []
    _forbid_text_path(monkeypatch)
    _install_codex_account(monkeypatch, token_calls)
    _install_upstream(monkeypatch, sent)

    response = responses.handle({"model": "gpt-5.5", "input": "read a.py", "tools": [FUNCTION_TOOL]})

    assert response["output"] == [CALL_ITEM]
    assert token_calls == [{"source_type": "codex"}]
    assert sent[0]["tools"] == [FUNCTION_TOOL]
    assert sent[0]["store"] is False and sent[0]["stream"] is True


def test_function_tool_stream_passes_upstream_events_through(monkeypatch):
    token_calls, sent = [], []
    _forbid_text_path(monkeypatch)
    _install_codex_account(monkeypatch, token_calls)
    _install_upstream(monkeypatch, sent)

    events = list(responses.handle({"model": "gpt-5.5", "input": "read a.py", "tools": [FUNCTION_TOOL], "stream": True}))

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[-1]["response"]["output"] == [CALL_ITEM]


def test_function_tool_without_codex_account_keeps_existing_text_fallback(monkeypatch):
    token_calls, seen = [], {}
    _install_codex_account(monkeypatch, token_calls)
    monkeypatch.setattr(account_service, "get_text_access_token", lambda **kwargs: token_calls.append(kwargs) or "")

    class FakeTextBackend:
        account_email = "web@example.com"

    def fake_stream_text_deltas(backend, request):
        seen["messages"] = request.messages
        yield "工具不可用"

    monkeypatch.setattr(responses, "text_backend", lambda: FakeTextBackend())
    monkeypatch.setattr(responses, "stream_text_deltas", fake_stream_text_deltas)

    response = responses.handle({"input": f"read a.py {uuid.uuid4().hex}", "tools": [FUNCTION_TOOL]})

    assert response["output"][0]["content"][0]["text"] == "工具不可用"
    assert seen["messages"][0]["content"] == responses.TOOL_UNAVAILABLE_SYSTEM_MESSAGE


def test_request_without_client_tools_never_asks_for_codex_account(monkeypatch):
    token_calls = []
    _install_codex_account(monkeypatch, token_calls)

    class FakeTextBackend:
        account_email = "web@example.com"

    monkeypatch.setattr(responses, "text_backend", lambda: FakeTextBackend())
    monkeypatch.setattr(responses, "stream_text_deltas", lambda backend, request: iter(["hi"]))

    response = responses.handle({"input": f"hello {uuid.uuid4().hex}"})

    assert response["output"][0]["content"][0]["text"] == "hi"
    assert {"source_type": "codex"} not in token_calls
