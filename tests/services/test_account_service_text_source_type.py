from services.account_service import account_service


def _install_accounts(monkeypatch):
    accounts = {
        "tok-web": {"access_token": "tok-web", "status": "正常", "source_type": "web"},
        "tok-codex": {"access_token": "tok-codex", "status": "正常", "source_type": "codex"},
    }
    monkeypatch.setattr(account_service, "_accounts", accounts)
    monkeypatch.setattr(account_service, "_index", 0)
    monkeypatch.setattr(account_service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    monkeypatch.setattr(account_service, "_is_account_selectable", lambda account, **kwargs: True)
    monkeypatch.setattr(account_service, "ensure_access_token", lambda token, **kwargs: token)


def test_text_access_token_filters_by_codex_source_type(monkeypatch):
    _install_accounts(monkeypatch)

    picked = {account_service.get_text_access_token(source_type="codex") for _ in range(4)}

    assert picked == {"tok-codex"}


def test_text_access_token_without_source_type_still_uses_every_account(monkeypatch):
    _install_accounts(monkeypatch)

    picked = {account_service.get_text_access_token() for _ in range(4)}

    assert picked == {"tok-web", "tok-codex"}


def test_text_access_token_returns_empty_when_no_codex_account(monkeypatch):
    _install_accounts(monkeypatch)
    monkeypatch.setattr(
        account_service,
        "_accounts",
        {"tok-web": {"access_token": "tok-web", "status": "正常", "source_type": "web"}},
    )

    assert account_service.get_text_access_token(source_type="codex") == ""
