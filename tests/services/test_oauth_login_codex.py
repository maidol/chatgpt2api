from urllib.parse import parse_qs, urlparse

from services import oauth_login_service as module
from services.oauth_login_service import OAuthLoginService

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"


class FakeResponse:
    status_code = 200
    text = '{"access_token": "at-new"}'

    def json(self):
        return {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "id-new"}


class FakeSession:
    calls = []

    def __init__(self, **kwargs):
        pass

    def post(self, url, **kwargs):
        FakeSession.calls.append({"url": url, **kwargs})
        return FakeResponse()

    def close(self):
        pass


def _query(url):
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _finish(monkeypatch, client):
    FakeSession.calls = []
    monkeypatch.setattr(module.requests, "Session", FakeSession)
    service = OAuthLoginService()
    started = service.start("", client) if client else service.start("")
    state = _query(started["authorize_url"])["state"]
    verifier = service._sessions[started["session_id"]]["code_verifier"]
    callback = f"{started['redirect_uri_prefix']}?code=the-code&state={state}"
    return service.finish(started["session_id"], callback), FakeSession.calls, verifier


def test_codex_start_builds_codex_cli_authorize_url():
    started = OAuthLoginService().start("", "codex")

    parsed = urlparse(started["authorize_url"])
    query = _query(started["authorize_url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.openai.com/oauth/authorize"
    assert query["client_id"] == CODEX_CLIENT_ID
    assert query["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert query["codex_cli_simplified_flow"] == "true"
    assert query["id_token_add_organizations"] == "true"
    assert query["code_challenge_method"] == "S256"
    assert query["scope"] == "openid profile email offline_access"
    assert "audience" not in query
    assert started["redirect_uri_prefix"] == "http://localhost:1455/auth/callback"
    assert started["client"] == "codex"


def test_default_start_is_still_platform_web_login():
    started = OAuthLoginService().start("")

    parsed = urlparse(started["authorize_url"])
    query = _query(started["authorize_url"])
    assert parsed.path == "/api/accounts/authorize"
    assert query["client_id"] == PLATFORM_CLIENT_ID
    assert started["redirect_uri_prefix"] == "https://platform.openai.com/auth/callback"
    assert started["client"] == "web"


def test_codex_finish_exchanges_code_with_codex_client(monkeypatch):
    tokens, calls, verifier = _finish(monkeypatch, "codex")

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://auth.openai.com/oauth/token"
    assert "json" not in call
    assert call["data"] == {
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": "the-code",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "code_verifier": verifier,
    }
    assert call["headers"]["originator"] == "codex-tui"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert tokens == {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "id-new", "client": "codex"}


def test_web_finish_exchange_is_unchanged(monkeypatch):
    tokens, calls, verifier = _finish(monkeypatch, None)

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    assert call["json"]["client_id"] == PLATFORM_CLIENT_ID
    assert call["json"]["redirect_uri"] == "https://platform.openai.com/auth/callback"
    assert call["json"]["code_verifier"] == verifier
    assert tokens["client"] == "web"
