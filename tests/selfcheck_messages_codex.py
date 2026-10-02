"""/v1/messages 接 codex 通道的自检：先确认 15 条全绿，再逐个注入错误改法，每个都必须变红。

用法（在仓库根目录）：
    .venv/bin/python tests/selfcheck_messages_codex.py

最后一行三选一：
    SELFCHECK: ALL 18 MUTANTS RED          -> exit 0，通过
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
BACKUP_DIR = REPO / ".selfcheck-messages-backup"
TEST_FILES = [
    "tests/services/protocol/test_anthropic_codex_bridge.py",
    "tests/services/protocol/test_anthropic_messages_codex_route.py",
    "tests/api/test_messages_route_codex_tools.py",
]
EXPECTED_PASSED = 15

BRIDGE = "services/protocol/anthropic_codex_bridge.py"
MESSAGES = "services/protocol/anthropic_v1_messages.py"

# (名字, 文件, 找到, 替换成)。「找到」在文件里必须恰好出现一次。
MUTANTS = [
    ("M1 handle 不接 codex 分支", MESSAGES,
     "    if has_client_tools(body):\n", "    if False:\n"),
    ("M2 不带工具的请求也去要 codex 号（错误改法）", MESSAGES,
     "    if has_client_tools(body):\n", "    if True:\n"),
    ("M3 没有 codex 号时不回落旧路径", MESSAGES,
     "        if codex_result is not None:\n", "        if True:\n"),
    ("M4 system 不转成 developer 消息", BRIDGE,
     '    if system:\n        items.append({"type": "message", "role": "developer", "content": system})\n',
     "    if False:\n        pass\n"),
    ("M5 不过滤 billing header", BRIDGE,
     "if text and not text.startswith(BILLING_HEADER_PREFIX)]", "if text]"),
    ("M6 tool_result 不转 function_call_output", BRIDGE,
     '        if block.get("type") == "tool_result":\n            output, images = _tool_result_output(block)\n',
     '        if False:\n            output, images = _tool_result_output(block)\n'),
    ("M7 tool_result 里的图片丢掉", BRIDGE,
     "    parts.extend(tool_images)\n", ""),
    ("M8 call_id 不用 tool_use 的 id", BRIDGE,
     '                "call_id": str(block.get("id") or ""),\n',
     '                "call_id": "fc_" + str(block.get("id") or ""),\n'),
    ("M9 arguments 不是 JSON 字符串", BRIDGE,
     '                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),\n',
     '                "arguments": str(block.get("input") or {}),\n'),
    ("M10 工具 strict=True", BRIDGE,
     '            "strict": False,\n', '            "strict": True,\n'),
    ("M11 object schema 不补 properties", BRIDGE,
     '    if schema.get("type") == "object" and "properties" not in schema:\n',
     "    if False:\n"),
    ("M12 tool_choice any 映射错", BRIDGE,
     '    if kind == "any":\n        return "required"\n', '    if kind == "any":\n        return "auto"\n'),
    ("M13 claude 模型名原样透传", BRIDGE,
     '    return model if model.lower().startswith("gpt-") else CODEX_RESPONSES_MODEL\n',
     "    return model or CODEX_RESPONSES_MODEL\n"),
    ("M14 有工具调用也报 end_turn", BRIDGE,
     '    elif any(block["type"] == "tool_use" for block in content):\n',
     "    elif False:\n"),
    ("M15 缓存命中不从 input_tokens 里扣", BRIDGE,
     '            "input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached),\n',
     '            "input_tokens": int(usage.get("input_tokens") or 0),\n'),
    ("M16 不删 Read 的 pages 空串", BRIDGE,
     '    if name == "Read" and parsed.get("pages") == "":\n', "    if False:\n"),
    ("M17 response.failed 不报错", BRIDGE,
     '        if event_type in {"error", "response.failed"}:\n', "        if False:\n"),
    ("M18 截断时不用 done 项补 output", BRIDGE,
     '    if not response.get("output") and done_items:\n', "    if False:\n"),
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
    env.setdefault("DATABASE_URL", "sqlite:////tmp/chatgpt2api-messages-codex-selfcheck.db")
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
    if not (REPO / MESSAGES).exists():
        print("SELFCHECK: NOT RUN (请在 chatgpt2api 仓库根目录运行)")
        return 3
    restore_leftovers()
    missing = [name for name in [*TEST_FILES, BRIDGE] if not (REPO / name).exists()]
    if missing:
        print(f"SELFCHECK: NOT RUN (缺文件: {', '.join(missing)})")
        return 3

    code, output = run_tests()
    summary = last_summary(output)
    print(f"baseline: {summary}")
    if code != 0 or f"{EXPECTED_PASSED} passed" not in summary:
        print(output[-3000:])
        print(f"SELFCHECK: NOT RUN (改动后的基线不是 {EXPECTED_PASSED} passed，先修到全绿)")
        return 3

    green, not_run = [], []
    try:
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
    except Terminated:
        print("SELFCHECK: NOT RUN (被 SIGTERM 中断，文件已还原)")
        return 2

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
