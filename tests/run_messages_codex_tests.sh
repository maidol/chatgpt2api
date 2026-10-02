#!/usr/bin/env bash
# 跑 /v1/messages 接 codex 通道的 3 个测试文件。
# 写成脚本是为了绕开会改写/截断 pytest 输出的 shell 钩子；直接敲 pytest 可能只剩一行摘要。
cd "$(dirname "$0")/.." || exit 2
export CHATGPT2API_AUTH_KEY="${CHATGPT2API_AUTH_KEY:-test}"
export DATABASE_URL="${DATABASE_URL:-sqlite:////tmp/chatgpt2api-messages-codex-test.db}"
exec .venv/bin/python -m pytest -p no:cacheprovider -q -rA --continue-on-collection-errors \
  tests/services/protocol/test_anthropic_codex_bridge.py \
  tests/services/protocol/test_anthropic_messages_codex_route.py \
  tests/api/test_messages_route_codex_tools.py
