import base64
import json

import curl_cffi.requests

from services.account_service import account_service

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"


def _jwt(claims):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class FakeResponse:
    status_code = 200
    text = '{"access_token": "at-new"}'

    def json(self):
        return {"access_token": "at-new", "refresh_token": "rt-new"}


class FakeSession:
    calls = []

    def __init__(self, **kwargs):
        pass

    def post(self, url, **kwargs):
        FakeSession.calls.append({"url": url, **kwargs})
        return FakeResponse()

    def close(self):
        pass


def _refresh(monkeypatch, account):
    FakeSession.calls = []
    monkeypatch.setattr(curl_cffi.requests, "Session", FakeSession)
    result = account_service._request_access_token_refresh("rt-old", account)
    assert result["access_token"] == "at-new"
    assert len(FakeSession.calls) == 1
    return FakeSession.calls[0]


def test_codex_oauth_account_refreshes_with_codex_client(monkeypatch):
    call = _refresh(monkeypatch, {"access_token": "opaque", "oauth_client_id": CODEX_CLIENT_ID})

    assert call["data"]["client_id"] == CODEX_CLIENT_ID
    assert call["data"]["scope"] == "openid profile email"
    assert call["headers"]["originator"] == "codex-tui"


def test_imported_codex_token_refreshes_with_its_own_client_claim(monkeypatch):
    call = _refresh(monkeypatch, {"access_token": _jwt({"client_id": CODEX_CLIENT_ID})})

    assert call["data"]["client_id"] == CODEX_CLIENT_ID
    assert call["data"]["scope"] == "openid profile email"


def test_web_account_refresh_is_unchanged(monkeypatch):
    call = _refresh(monkeypatch, {"access_token": _jwt({"client_id": PLATFORM_CLIENT_ID})})

    assert call["data"] == {"grant_type": "refresh_token", "refresh_token": "rt-old", "client_id": PLATFORM_CLIENT_ID}
    assert "originator" not in call["headers"]


def test_account_without_any_client_hint_keeps_platform_client(monkeypatch):
    call = _refresh(monkeypatch, {"access_token": "opaque", "source_type": "codex"})

    assert call["data"]["client_id"] == PLATFORM_CLIENT_ID
    assert "scope" not in call["data"]
