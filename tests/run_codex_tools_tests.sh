#!/usr/bin/env bash
# 跑 codex 原生工具调用的 5 个测试文件。
# 写成脚本是为了绕开会改写/截断 pytest 输出的 shell 钩子；直接敲 pytest 可能只剩一行摘要。
cd "$(dirname "$0")/.." || exit 2
export CHATGPT2API_AUTH_KEY="${CHATGPT2API_AUTH_KEY:-test}"
export DATABASE_URL="${DATABASE_URL:-sqlite:////tmp/chatgpt2api-codex-tools-test.db}"
exec .venv/bin/python -m pytest -p no:cacheprovider -q -rA --continue-on-collection-errors \
  tests/services/test_account_service_text_source_type.py \
  tests/services/test_openai_backend_codex_responses.py \
  tests/services/protocol/test_codex_tool_passthrough.py \
  tests/services/protocol/test_openai_v1_response_codex_tools.py \
  tests/api/test_responses_route_codex_tools.py
