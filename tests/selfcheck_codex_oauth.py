"""Codex OAuth 添加账号的自检：先确认 11 条后端测试全绿、前端契约 9 项全过，再逐个注入错误改法，每个都必须变红。

用法（在仓库根目录，web-vue/node_modules 已装好）：
    .venv/bin/python tests/selfcheck_codex_oauth.py

最后一行三选一：
    SELFCHECK: ALL 20 MUTANTS RED          -> exit 0，通过
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
BACKUP_DIR = REPO / ".selfcheck-oauth-backup"
TEST_FILES = [
    "tests/services/test_oauth_login_codex.py",
    "tests/api/test_oauth_finish_codex_source.py",
    "tests/services/test_account_refresh_client_id.py",
]
UI_CHECK = "tests/check_codex_oauth_ui.mjs"
EXPECTED_PASSED = 11

LOGIN = "services/oauth_login_service.py"
API = "api/accounts.py"
ACCOUNT = "services/account_service.py"
UI_API = "web-vue/src/api/accountImports.ts"
UI_RUNTIME = "web-vue/src/views/accounts/accountImportRuntime.ts"
UI_PAGE = "web-vue/src/views/accounts/useAccountsPage.ts"
UI_VIEW = "web-vue/src/views/Accounts.vue"

# (名字, 跑哪组检查, 文件, 找到, 替换成)。「找到」在文件里必须恰好出现一次。
MUTANTS = [
    ("B1 start 忽略 client，永远网页登录", "py", LOGIN,
     '        client = "codex" if str(client or "").strip().lower() == "codex" else "web"\n',
     '        client = "web"\n'),
    ("B2 默认改成 codex（错误改法）", "py", LOGIN,
     '        client = "codex" if str(client or "").strip().lower() == "codex" else "web"\n',
     '        client = "codex"\n'),
    ("B3 codex 授权链接用 platform client_id", "py", LOGIN,
     '                "response_type": "code",\n                "client_id": codex_oauth_client_id,\n',
     '                "response_type": "code",\n                "client_id": platform_oauth_client_id,\n'),
    ("B4 codex 回调地址用 platform 的", "py", LOGIN,
     "            redirect_uri = codex_oauth_redirect_uri\n",
     "            redirect_uri = platform_oauth_redirect_uri\n"),
    ("B5 去掉 codex_cli_simplified_flow", "py", LOGIN,
     '                "codex_cli_simplified_flow": "true",\n', ""),
    ("B6 finish 不把 client 传给换 token", "py", LOGIN,
     '            session.get("redirect_uri") or platform_oauth_redirect_uri,\n            client,\n',
     '            session.get("redirect_uri") or platform_oauth_redirect_uri,\n'),
    ("B7 codex 换 token 用 JSON 不用表单", "py", LOGIN,
     "                    data={\n", "                    json={\n"),
    ("B8 codex 换 token 用 platform client_id", "py", LOGIN,
     '                        "grant_type": "authorization_code",\n                        "client_id": codex_oauth_client_id,\n',
     '                        "grant_type": "authorization_code",\n                        "client_id": platform_oauth_client_id,\n'),
    ("B9 finish 落盘永远是 web", "py", API,
     '            "source_type": "codex" if is_codex else "web",\n',
     '            "source_type": "web",\n'),
    ("B10 finish 不记 oauth_client_id", "py", API,
     '            payload["oauth_client_id"] = codex_oauth_client_id\n',
     "            pass\n"),
    ("B11 start 接口不转发 client", "py", API,
     "oauth_login_service.start, body.email_hint, body.client)",
     "oauth_login_service.start, body.email_hint)"),
    ("B12 刷新不认记下的 oauth_client_id", "py", ACCOUNT,
     '        stored = str(account.get("oauth_client_id") or "").strip()\n',
     '        stored = ""\n'),
    ("B13 刷新不认 AT 的 client_id 声明", "py", ACCOUNT,
     "        return claimed or self._OAUTH_CLIENT_ID\n",
     "        return self._OAUTH_CLIENT_ID\n"),
    ("B14 刷新按 source_type 推断（错误改法）", "py", ACCOUNT,
     "        return claimed or self._OAUTH_CLIENT_ID\n",
     '        return claimed or (codex_oauth_client_id if account.get("source_type") == "codex" else self._OAUTH_CLIENT_ID)\n'),
    ("B15 codex 刷新不带 scope", "py", ACCOUNT,
     '            data["scope"] = codex_oauth_refresh_scope\n',
     "            pass\n"),
    ("F1 请求体不带 client", "ui", UI_API,
     "      { email_hint: emailHint, client },\n",
     "      { email_hint: emailHint },\n"),
    ("F2 runtime 不传 oauthClient", "ui", UI_RUNTIME,
     "accountImportsApi.startOAuthLogin(oauthEmailHint.value, oauthClient.value)",
     "accountImportsApi.startOAuthLogin(oauthEmailHint.value)"),
    ("F3 runtime 不返回 oauthClient", "ui", UI_RUNTIME,
     "    oauthRedirectUriPrefix,\n    oauthClient,\n    manualTokenText,\n",
     "    oauthRedirectUriPrefix,\n    manualTokenText,\n"),
    ("F4 页面 composable 不返回 oauthClient", "ui", UI_PAGE,
     "    oauthRedirectUriPrefix,\n    oauthClient,\n    manualTokenText,\n",
     "    oauthRedirectUriPrefix,\n    manualTokenText,\n"),
    ("F5 下拉框选中后不写回", "ui", UI_VIEW,
     "@update:model-value=\"oauthClient = $event === 'codex' ? 'codex' : 'web'\"",
     "@update:model-value=\"() => {}\""),
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


def run_py() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("CHATGPT2API_AUTH_KEY", "selfcheck")
    env.setdefault("DATABASE_URL", "sqlite:////tmp/chatgpt2api-codex-oauth-selfcheck.db")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--continue-on-collection-errors", *TEST_FILES],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def run_ui() -> tuple[int, str]:
    node = shutil.which("node")
    if not node:
        return 127, "node not found"
    proc = subprocess.run([node, UI_CHECK], cwd=REPO, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def last_summary(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "<no output>"


def is_red(kind: str, code: int, summary: str) -> bool:
    if kind == "py":
        return code == 1 and ("failed" in summary or "error" in summary)
    return code == 1 and summary.startswith("UI CONTRACT:") and "FAILED" in summary


def main() -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    if not (REPO / LOGIN).exists() or not (REPO / UI_VIEW).exists():
        print("SELFCHECK: NOT RUN (请在 chatgpt2api 仓库根目录运行)")
        return 3
    restore_leftovers()
    missing = [name for name in [*TEST_FILES, UI_CHECK] if not (REPO / name).exists()]
    if missing:
        print(f"SELFCHECK: NOT RUN (缺测试文件: {', '.join(missing)})")
        return 3
    if not (REPO / "web-vue/node_modules/typescript").exists():
        print("SELFCHECK: NOT RUN (web-vue/node_modules 没装：先在 web-vue 里 npm ci)")
        return 3

    code, output = run_py()
    summary = last_summary(output)
    print(f"baseline py: {summary}")
    if code != 0 or f"{EXPECTED_PASSED} passed" not in summary:
        print(output[-3000:])
        print(f"SELFCHECK: NOT RUN (改动后的基线不是 {EXPECTED_PASSED} passed，先修到全绿)")
        return 3
    code, output = run_ui()
    summary = last_summary(output)
    print(f"baseline ui: {summary}")
    if code != 0 or summary != "UI CONTRACT: ALL 9 CHECKS PASSED":
        print(output[-3000:])
        print("SELFCHECK: NOT RUN (前端契约基线没有全过，先修到 ALL 9 CHECKS PASSED)")
        return 3

    green, not_run = [], []
    try:
        for name, kind, rel, find, replace in MUTANTS:
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
                code, output = run_py() if kind == "py" else run_ui()
            finally:
                shutil.copy2(backup, path)
                shutil.rmtree(BACKUP_DIR)
            summary = last_summary(output)
            if is_red(kind, code, summary):
                print(f"{name}: RED   ({summary})")
            elif code == 0:
                print(f"{name}: GREEN ({summary})  <-- 没拦住这个错误改法")
                green.append(name)
            else:
                print(f"{name}: NOT RUN (exit {code}: {summary})")
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
