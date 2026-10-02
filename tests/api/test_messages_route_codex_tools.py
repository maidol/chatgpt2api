import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai
from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import anthropic_v1_messages as messages

READ_TOOL = {"name": "Read", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}
CALL_ITEM = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Read",
             "arguments": "{\"file_path\": \"a.py\"}", "status": "completed"}


class TextPathUsed(BaseException):
    """BaseException so no `except Exception` on the path can swallow it."""


def _client(monkeypatch):
    def forbidden():
        raise TextPathUsed("tool request fell through to the ChatGPT web text path")

    def fake_iter(self, payload):
        yield {"type": "response.output_item.done", "output_index": 0, "item": CALL_ITEM}
        yield {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": []}}

    monkeypatch.setattr(ai, "require_identity", lambda authorization: {"id": "k", "name": "test", "role": "admin"})
    monkeypatch.setattr(ai, "check_request", lambda text: None)
    monkeypatch.setattr(messages, "text_backend", forbidden)
    monkeypatch.setattr(account_service, "get_text_access_token",
                        lambda **kwargs: "tok-codex" if kwargs.get("source_type") == "codex" else "")
    monkeypatch.setattr(account_service, "get_account", lambda token: {"email": "codex@example.com", "source_type": "codex"})
    monkeypatch.setattr(OpenAIBackendAPI, "iter_codex_responses_events", fake_iter, raising=False)
    app = FastAPI()
    app.include_router(ai.create_router())
    return TestClient(app)


BODY = {"model": "gpt-5.6", "max_tokens": 1024, "tools": [READ_TOOL],
        "messages": [{"role": "user", "content": "read a.py"}]}


def test_messages_route_returns_tool_use_json(monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/v1/messages", headers={"x-api-key": "x", "anthropic-version": "2023-06-01"}, json=BODY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stop_reason"] == "tool_use"
    assert body["content"] == [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "a.py"}}]
    assert "_account_email" not in body


def test_messages_route_streams_anthropic_tool_use_events(monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/v1/messages", headers={"x-api-key": "x"}, json={**BODY, "stream": True})

    assert resp.status_code == 200, resp.text
    lines = resp.text.splitlines()
    events = [line.removeprefix("event: ") for line in lines if line.startswith("event: ")]
    data = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")]
    assert events == ["message_start", "content_block_start", "content_block_delta", "content_block_stop",
                      "message_delta", "message_stop"]
    assert data[2]["delta"] == {"type": "input_json_delta", "partial_json": "{\"file_path\": \"a.py\"}"}
    assert data[4]["delta"]["stop_reason"] == "tool_use"
    assert all("_account_email" not in item for item in data)
