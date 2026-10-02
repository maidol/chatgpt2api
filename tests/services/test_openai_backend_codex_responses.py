import json
import urllib.request

import pytest

from services import openai_backend_api
from services.openai_backend_api import OpenAIBackendAPI


class UpstreamCalled(BaseException):
    """BaseException so no `except Exception` on the path can swallow it."""


class FakeRaw:
    def __init__(self, lines):
        self.headers = {"content-type": "text/event-stream"}
        self.status = 200
        self._lines = [line.encode() for line in lines]

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def read(self):
        return b"".join(self._lines)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(event):
    return [f"data: {json.dumps(event)}\n", "\n"]


def test_codex_responses_refuses_non_codex_account(monkeypatch):
    monkeypatch.setattr(openai_backend_api.account_service, "get_account", lambda token: {"source_type": "web"})

    def forbidden_urlopen(*args, **kwargs):
        raise UpstreamCalled("urlopen must not run for a web account")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    backend = OpenAIBackendAPI(access_token="tok-web")

    with pytest.raises(RuntimeError, match="requires a codex source account"):
        list(backend.iter_codex_responses_events({"model": "gpt-5.5", "input": []}))


def test_codex_responses_posts_payload_verbatim_and_yields_events(monkeypatch):
    monkeypatch.setattr(openai_backend_api.account_service, "get_account", lambda token: {"source_type": "codex"})
    captured = {}
    upstream_events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
    ]

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        lines = []
        for event in upstream_events:
            lines.extend(_sse(event))
        return FakeRaw(lines)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = {
        "model": "gpt-5.5",
        "store": False,
        "stream": True,
        "instructions": "x",
        "input": [],
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
    }
    backend = OpenAIBackendAPI(access_token="tok-codex")

    events = list(backend.iter_codex_responses_events(payload))

    assert captured["url"].endswith("/backend-api/codex/responses")
    assert captured["body"] == payload
    assert captured["auth"] == "Bearer tok-codex"
    assert captured["timeout"] == openai_backend_api.CODEX_TEXT_STREAM_TIMEOUT_SECS
    assert [event["type"] for event in events] == ["response.created", "response.completed"]
