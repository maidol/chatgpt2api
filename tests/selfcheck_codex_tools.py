"""codex 原生工具调用的自检：先确认 16 条全绿，再逐个注入错误改法，每个都必须变红。

用法（在仓库根目录）：
    .venv/bin/python tests/selfcheck_codex_tools.py

最后一行三选一：
    SELFCHECK: ALL 11 MUTANTS RED          -> exit 0，通过
    SELFCHECK: <n> MUTANT(S) STAYED GREEN  -> exit 1，测试守不住这些错误改法
    SELFCHECK: NOT RUN (<原因>)            -> exit 2/3，没跑成，不能当作通过
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path.cwd()
BACKUP_DIR = REPO / ".selfcheck-backup"
TEST_FILES = [
    "tests/services/test_account_service_text_source_type.py",
    "tests/services/test_openai_backend_codex_responses.py",
    "tests/services/protocol/test_codex_tool_passthrough.py",
    "tests/services/protocol/test_openai_v1_response_codex_tools.py",
    "tests/api/test_responses_route_codex_tools.py",
]
EXPECTED_PASSED = 16

ACCOUNT = "services/account_service.py"
BACKEND = "services/openai_backend_api.py"
PASSTHROUGH = "services/protocol/codex_tool_passthrough.py"
ROUTE = "services/protocol/openai_v1_response.py"

# (名字, 文件, 找到, 替换成)。「找到」在文件里必须恰好出现一次。
MUTANTS = [
    ("M1 账号筛选不看 source_type", ACCOUNT,
     "                       and self._account_matches_source_type(account, source_type)\n", ""),
    ("M2 后端不校验 codex 号", BACKEND,
     "        self._ensure_codex_source_account()\n        path = \"/backend-api/codex/responses\"\n        request = urllib.request.Request(",
     "        path = \"/backend-api/codex/responses\"\n        request = urllib.request.Request("),
    ("M3 store 不强制 False", PASSTHROUGH,
     "    payload[\"store\"] = False\n", "    payload[\"store\"] = True\n"),
    ("M4 不丢弃值为 None 的字段", PASSTHROUGH,
     "        if value is not None and not str(key).startswith(\"_\")\n",
     "        if not str(key).startswith(\"_\")\n"),
    ("M5 不剥上游不支持的字段", PASSTHROUGH,
     "    for key in CODEX_UNSUPPORTED_FIELDS:\n", "    for key in ():\n"),
    ("M6 completed.output 为空时不补齐", PASSTHROUGH,
     "                    if not response.get(\"output\") and done_items:\n",
     "                    if False:\n"),
    ("M7 选号不限定 codex", PASSTHROUGH,
     "account_service.get_text_access_token(source_type=\"codex\")",
     "account_service.get_text_access_token()"),
    ("M8 没有 codex 号时不回落旧路径", PASSTHROUGH,
     "        logger.warning({\"event\": \"codex_tool_passthrough_no_account\"})\n        return None\n",
     "        logger.warning({\"event\": \"codex_tool_passthrough_no_account\"})\n        return iter(())\n"),
    ("M9 路由不接 codex 分支", ROUTE,
     "    if has_unsupported_response_tools(body):\n        codex_events = codex_tool_response_events(body)\n",
     "    if False:\n        codex_events = codex_tool_response_events(body)\n"),
    ("M10 所有请求都走 codex（错误改法）", ROUTE,
     "    if has_unsupported_response_tools(body):\n        codex_events = codex_tool_response_events(body)\n",
     "    if True:\n        codex_events = codex_tool_response_events(body)\n"),
    ("M11 用完不关闭后端", PASSTHROUGH,
     "    finally:\n        backend.close()\n", "    finally:\n        pass\n"),
]


class Terminated(Exception):
    pass


def _on_sigterm(signum, frame):
    raise Terminated()


def restore_leftovers() -> None:
    """上次被 kill -9 时 finally 没跑，变异还留在文件里：先从备份还原。"""
    if not BACKUP_DIR.exists():
        return
    for backup in BACKUP_DIR.rglob("*"):
        if backup.is_file():
            target = REPO / backup.relative_to(BACKUP_DIR)
            shutil.copy2(backup, target)
            print(f"restored leftover mutant: {target.relative_to(REPO)}")
    shutil.rmtree(BACKUP_DIR)


def run_tests() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("CHATGPT2API_AUTH_KEY", "selfcheck")
    env.setdefault("DATABASE_URL", "sqlite:////tmp/chatgpt2api-codex-tools-selfcheck.db")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--continue-on-collection-errors", *TEST_FILES],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def last_summary(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "<no output>"


def main() -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    if not (REPO / ROUTE).exists():
        print("SELFCHECK: NOT RUN (请在 chatgpt2api 仓库根目录运行)")
        return 3
    restore_leftovers()
    missing = [name for name in TEST_FILES if not (REPO / name).exists()]
    if missing:
        print(f"SELFCHECK: NOT RUN (缺测试文件: {', '.join(missing)})")
        return 3

    code, output = run_tests()
    summary = last_summary(output)
    print(f"baseline: {summary}")
    if code != 0 or f"{EXPECTED_PASSED} passed" not in summary:
        print(output[-3000:])
        print(f"SELFCHECK: NOT RUN (改动后的基线不是 {EXPECTED_PASSED} passed，先修到全绿)")
        return 3

    green, not_run = [], []
    for name, rel, find, replace in MUTANTS:
        path = REPO / rel
        source = path.read_text(encoding="utf-8")
        if source.count(find) != 1:
            print(f"{name}: NOT RUN (锚点出现 {source.count(find)} 次，应为 1 次——实现和计划不一致)")
            not_run.append(name)
            continue
        backup = BACKUP_DIR / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        try:
            path.write_text(source.replace(find, replace), encoding="utf-8")
            code, output = run_tests()
        finally:
            shutil.copy2(backup, path)
            shutil.rmtree(BACKUP_DIR)
        summary = last_summary(output)
        if code == 1 and ("failed" in summary or "error" in summary):
            print(f"{name}: RED   ({summary})")
        elif code == 0:
            print(f"{name}: GREEN ({summary})  <-- 测试没拦住这个错误改法")
            green.append(name)
        else:
            print(f"{name}: NOT RUN (pytest exit {code}: {summary})")
            not_run.append(name)

    if not_run:
        print(f"SELFCHECK: NOT RUN ({len(not_run)} 个变异没跑成: {', '.join(not_run)})")
        return 2
    if green:
        print(f"SELFCHECK: {len(green)} MUTANT(S) STAYED GREEN: {', '.join(green)}")
        return 1
    print(f"SELFCHECK: ALL {len(MUTANTS)} MUTANTS RED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
