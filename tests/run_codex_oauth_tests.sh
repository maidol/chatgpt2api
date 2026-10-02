#!/usr/bin/env bash
# 跑 Codex OAuth 添加账号的 3 个测试文件。
# 写成脚本是为了绕开会改写/截断 pytest 输出的 shell 钩子；直接敲 pytest 可能只剩一行摘要。
cd "$(dirname "$0")/.." || exit 2
export CHATGPT2API_AUTH_KEY="${CHATGPT2API_AUTH_KEY:-test}"
export DATABASE_URL="${DATABASE_URL:-sqlite:////tmp/chatgpt2api-codex-oauth-test.db}"
exec .venv/bin/python -m pytest -p no:cacheprovider -q -rA --continue-on-collection-errors \
  tests/services/test_oauth_login_codex.py \
  tests/api/test_oauth_finish_codex_source.py \
  tests/services/test_account_refresh_client_id.py
