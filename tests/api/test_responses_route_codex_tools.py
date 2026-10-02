import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai
from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import openai_v1_response as responses

FUNCTION_TOOL = {
    "type": "function",
    "name": "read_file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
CALL_ITEM = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file",
             "arguments": "{\"path\": \"a.py\"}", "status": "completed"}


class TextPathUsed(BaseException):
    """BaseException so no `except Exception` on the path can swallow it."""


def _client(monkeypatch):
    sent = []

    def forbidden_text_backend():
        raise TextPathUsed("tool request fell through to the ChatGPT web text path")

    def fake_iter_codex_responses_events(self, payload):
        sent.append(payload)
        yield {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}
        yield {"type": "response.output_item.done", "output_index": 0, "item": CALL_ITEM}
        yield {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": []}}

    monkeypatch.setattr(ai, "require_identity", lambda authorization: {"id": "k", "name": "test", "role": "admin"})
    monkeypatch.setattr(ai, "check_request", lambda text: None)
    monkeypatch.setattr(responses, "text_backend", forbidden_text_backend)
    monkeypatch.setattr(account_service, "get_text_access_token",
                        lambda **kwargs: "tok-codex" if kwargs.get("source_type") == "codex" else "")
    monkeypatch.setattr(account_service, "get_account", lambda token: {"email": "codex@example.com", "source_type": "codex"})
    monkeypatch.setattr(OpenAIBackendAPI, "iter_codex_responses_events", fake_iter_codex_responses_events, raising=False)
    app = FastAPI()
    app.include_router(ai.create_router())
    return TestClient(app), sent


def test_responses_route_returns_function_call_json(monkeypatch):
    client, sent = _client(monkeypatch)

    resp = client.post("/v1/responses", headers={"Authorization": "Bearer x"},
                       json={"model": "gpt-5.5", "input": "read a.py", "tools": [FUNCTION_TOOL]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["output"] == [CALL_ITEM]
    assert "_account_email" not in body
    assert sent[0]["tools"] == [FUNCTION_TOOL]
    assert "tool_choice" not in sent[0]


def test_responses_route_streams_function_call_events(monkeypatch):
    client, _sent = _client(monkeypatch)

    resp = client.post("/v1/responses", headers={"Authorization": "Bearer x"},
                       json={"model": "gpt-5.5", "input": "read a.py", "tools": [FUNCTION_TOOL], "stream": True})

    assert resp.status_code == 200, resp.text
    datas = [line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")]
    assert datas[-1] == "[DONE]"
    events = [json.loads(item) for item in datas[:-1]]
    assert [event["type"] for event in events] == ["response.created", "response.output_item.done", "response.completed"]
    assert events[-1]["response"]["output"] == [CALL_ITEM]
    assert "_account_email" not in events[-1]
