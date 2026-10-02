from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import accounts
from services.account_service import account_service
from services.oauth_login_service import oauth_login_service


def _client(monkeypatch, client_kind):
    added = []
    started = []

    def fake_start(email_hint="", client="web"):
        started.append({"email_hint": email_hint, "client": client})
        return {"session_id": "s", "authorize_url": "https://x", "expires_in": "600",
                "redirect_uri_prefix": "https://x", "client": client}

    def fake_finish(session_id, callback):
        return {"access_token": "at-1", "refresh_token": "rt-1", "id_token": "id-1", "client": client_kind}

    def fake_add_account_items(items, return_items=True, **kwargs):
        added.extend(items)
        return {"added": 1, "skipped": 0}

    monkeypatch.setattr(accounts, "require_admin", lambda authorization: {"id": "admin", "role": "admin"})
    monkeypatch.setattr(oauth_login_service, "start", fake_start)
    monkeypatch.setattr(oauth_login_service, "finish", fake_finish)
    monkeypatch.setattr(account_service, "add_account_items", fake_add_account_items)
    monkeypatch.setattr(account_service, "get_account", lambda token: {"management_id": "acct_1"})
    app = FastAPI()
    app.include_router(accounts.create_router())
    return TestClient(app), added, started


def test_codex_oauth_finish_saves_codex_source_and_client_id(monkeypatch):
    client, added, _ = _client(monkeypatch, "codex")

    resp = client.post("/api/accounts/oauth/finish", headers={"Authorization": "Bearer x"},
                       json={"session_id": "s", "callback": "http://localhost:1455/auth/callback?code=c&state=s.n"})

    assert resp.status_code == 200, resp.text
    assert added[0]["source_type"] == "codex"
    assert added[0]["oauth_client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert added[0]["refresh_token"] == "rt-1"


def test_web_oauth_finish_still_saves_web_source(monkeypatch):
    client, added, _ = _client(monkeypatch, "web")

    resp = client.post("/api/accounts/oauth/finish", headers={"Authorization": "Bearer x"},
                       json={"session_id": "s", "callback": "https://platform.openai.com/auth/callback?code=c"})

    assert resp.status_code == 200, resp.text
    assert added[0]["source_type"] == "web"
    assert "oauth_client_id" not in added[0]


def test_oauth_start_forwards_client_choice(monkeypatch):
    client, _, started = _client(monkeypatch, "codex")

    resp = client.post("/api/accounts/oauth/start", headers={"Authorization": "Bearer x"},
                       json={"email_hint": "", "client": "codex"})
    default = client.post("/api/accounts/oauth/start", headers={"Authorization": "Bearer x"},
                          json={"email_hint": ""})

    assert resp.status_code == 200, resp.text
    assert default.status_code == 200, default.text
    assert started == [{"email_hint": "", "client": "codex"}, {"email_hint": "", "client": "web"}]
