"""Shared OpenAI OAuth protocol constants and request headers."""

from __future__ import annotations


auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"

# Codex CLI 的 OAuth 客户端：只有它签发的 token 能调 /backend-api/codex/responses。
# 取值与 sub2api internal/pkg/openai/oauth.go、openai_codex_identity.go 一致。
codex_oauth_client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
codex_oauth_authorize_url = f"{auth_base}/oauth/authorize"
codex_oauth_token_url = f"{auth_base}/oauth/token"
codex_oauth_redirect_uri = "http://localhost:1455/auth/callback"
codex_oauth_refresh_scope = "openid profile email"
codex_auth_user_agent = "codex-tui/0.146.0 (Ubuntu 22.4.0; x86_64) xterm-256color"
codex_auth_originator = "codex-tui"

user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'
sec_ch_ua_full_version_list = (
    '"Chromium";v="142.0.0.0", '
    '"Google Chrome";v="142.0.0.0", '
    '"Not_A Brand";v="99.0.0.0"'
)

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}
