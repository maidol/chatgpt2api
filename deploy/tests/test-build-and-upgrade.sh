#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_SOURCE="${ROOT_DIR}/deploy/build-and-upgrade.sh"
WORK_DIR=""

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}
trap cleanup EXIT

assert_file_contains() {
  local file="$1"
  local expected="$2"
  grep -F -- "$expected" "$file" >/dev/null || fail "${file} does not contain ${expected@Q}"
}

assert_file_equals() {
  local left="$1"
  local right="$2"
  cmp -s "$left" "$right" || fail "${left} differs from ${right}"
}

assert_log_order() {
  local log="$1"
  shift
  local previous=0
  local needle line
  for needle in "$@"; do
    line="$(grep -n -F -- "$needle" "$log" | head -n 1 | cut -d: -f1 || true)"
    [[ -n "$line" ]] || fail "command log is missing ${needle@Q}"
    (( line > previous )) || fail "command ${needle@Q} is out of order"
    previous="$line"
  done
}

if [[ ! -f "$SCRIPT_SOURCE" ]]; then
  fail "deploy/build-and-upgrade.sh is missing"
fi

make_fixture() {
  WORK_DIR="$(mktemp -d)"
  FIXTURE_SOURCE="$WORK_DIR/source"
  FIXTURE_INSTALL="$WORK_DIR/install"
  FIXTURE_FAKE_BIN="$WORK_DIR/fake-bin"
  FIXTURE_STATE="$WORK_DIR/state"
  local source="$FIXTURE_SOURCE"
  local install="$FIXTURE_INSTALL"
  local fake_bin="$FIXTURE_FAKE_BIN"
  local state="$FIXTURE_STATE"
  mkdir -p "$source/deploy" "$install/data" "$fake_bin" "$state"
  cp "$SCRIPT_SOURCE" "$source/deploy/build-and-upgrade.sh"
  cat >"$source/Dockerfile" <<'EOF'
FROM scratch
EOF
  printf '3.2.3\n' >"$source/VERSION"
  printf 'context\n' >"$source/.dockerignore"
  cat >"$source/docker-compose.yml" <<'EOF'
services:
  app:
    image: ${CHATGPT2API_IMAGE:-ghcr.io/yukkcat/chatgpt2api:latest}
EOF
  printf 'source\n' >"$source/README"
  chmod +x "$source/deploy/build-and-upgrade.sh"
  git -C "$source" init -q
  git -C "$source" config user.email test@example.invalid
  git -C "$source" config user.name test
  git -C "$source" add .
  git -C "$source" commit -qm fixture

  cat >"$install/.env" <<'EOF'
CHATGPT2API_AUTH_KEY=secret-value
CHATGPT2API_PORT=39001
CHATGPT2API_IMAGE=old:stable
DATABASE_MODE=sqlite
DATABASE_URL=
EOF
  printf '{"auth-key":"secret-value"}\n' >"$install/config.json"
  printf 'original-db\n' >"$install/data/chatgpt2api.db"
  printf 'original-wal\n' >"$install/data/chatgpt2api.db-wal"
  cat >"$install/docker-compose.yml" <<'EOF'
services:
  app:
    image: ${CHATGPT2API_IMAGE:-ghcr.io/yukkcat/chatgpt2api:latest}
    volumes:
      - chatgpt2api-runtime:/app
      - ./data:/app/data
      - ./config.json:/app/config.json
EOF
  printf 'seeded-runtime\n' >"$state/marker"
  printf 'running\n' >"$state/app_state"
  printf 'old-image-id\n' >"$state/image_id"
  printf '3.2.3\n' >"$state/app_version"
  printf '3.2.3\n' >"$state/seed_version"
  : >"$state/commands.log"

  cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
state="${FAKE_STATE_DIR:?}"
printf 'docker %q' "$1" >>"$state/commands.log"
for arg in "${@:2}"; do printf ' %q' "$arg" >>"$state/commands.log"; done
printf '\n' >>"$state/commands.log"

if [[ "${1:-}" == compose && "${2:-}" == version ]]; then
  printf 'Docker Compose version v2.30.0\n'
  exit 0
fi
if [[ "${1:-}" == inspect ]]; then
  format=""
  for arg in "$@"; do
    [[ "$arg" == --format=* ]] && format="${arg#--format=}"
  done
  if [[ "$format" == *State.Status* ]]; then
    tr -d '\n' <"$state/app_state"
    printf '\n'
  elif [[ "$format" == *Image* ]]; then
    tr -d '\n' <"$state/image_id"
    printf '\n'
  elif [[ "$format" == *Destination* || "$format" == *Mounts* ]]; then
    printf 'chatgpt2api-runtime\n'
  else
    printf '{}\n'
  fi
  exit 0
fi
if [[ "${1:-}" == exec ]]; then
  case "${*: -1}" in
    /app/VERSION) cat "$state/app_version" ;;
    /opt/chatgpt2api/VERSION) cat "$state/seed_version" ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [[ "${1:-}" == build ]]; then
  if [[ "${FAKE_BUILD_FAIL:-0}" == 1 ]]; then exit 41; fi
  exit 0
fi
if [[ "${1:-}" == tag ]]; then
  printf '%s\n' "${3:-}" >"$state/rollback_tag"
  exit 0
fi
if [[ "${1:-}" == run ]]; then
  if [[ "${FAKE_MARKER_FAIL:-0}" == 1 && ! -e "$state/marker_failed" ]]; then
    : >"$state/marker_failed"
    exit 42
  fi
  rm -f "$state/marker"
  exit 0
fi
if [[ "${1:-}" == compose ]]; then
  if printf '%s\n' "$@" | grep -qx stop; then
    printf 'stopped\n' >"$state/app_state"
    if [[ "${FAKE_STOP_FAIL:-0}" == 1 && ! -e "$state/stop_failed" ]]; then
      : >"$state/stop_failed"
      exit 45
    fi
  elif printf '%s\n' "$@" | grep -qx up; then
    printf 'running\n' >"$state/app_state"
    if grep -q '^CHATGPT2API_IMAGE=chatgpt2api:branch-' "$INSTALL_DIR/.env"; then
      printf 'new-wal\n' >"$INSTALL_DIR/data/chatgpt2api.db-wal"
      printf 'new-shm\n' >"$INSTALL_DIR/data/chatgpt2api.db-shm"
      if [[ "${FAKE_SIGNAL_ON_UP:-0}" == 1 ]]; then
        # Simulate an operator hangup: signal the top-level upgrade script (not its compose
        # subshell, which shares its command line) while it waits on us.
        pid="$PPID"
        target=""
        while [[ -n "$pid" && "$pid" -gt 1 ]]; do
          if ps -o args= -p "$pid" | grep -q '/deploy/build-and-upgrade\.sh'; then
            target="$pid"
          fi
          pid="$(ps -o ppid= -p "$pid" | tr -d ' ')"
        done
        [[ -n "$target" ]] && kill -TERM "$target"
      fi
    fi
  fi
  if [[ "${FAKE_COMPOSE_FAIL:-0}" == 1 && "${*}" == *" up "* && ! -e "$state/compose_failed" ]]; then
    : >"$state/compose_failed"
    exit 43
  fi
  exit 0
fi
exit 0
EOF
  cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
state="${FAKE_STATE_DIR:?}"
if [[ "${FAKE_HEALTH_FAIL_ALWAYS:-0}" == 1 ]]; then
  exit 22
fi
if [[ "${FAKE_HEALTH_FAIL:-0}" == 1 && ! -e "$state/health_failed" ]]; then
  : >"$state/health_failed"
  exit 22
fi
exit 0
EOF
  local real_git
  real_git="$(command -v git)"
  cat >"$fake_bin/git" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'git %s\\n' "\$*" >>"\${FAKE_STATE_DIR:?}/commands.log"
if [[ "\${FAKE_GIT_STATUS_FAIL:-0}" == 1 && " \$* " == *" status "* ]]; then
  printf 'fatal: detected dubious ownership\\n' >&2
  exit 128
fi
exec "$real_git" "\$@"
EOF
  cat >"$fake_bin/cp" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${FAKE_COPY_FAIL:-0}" == 1 && "${1:-}" == "${INSTALL_DIR}/data/chatgpt2api.db" ]]; then
  exit 44
fi
exec /bin/cp "$@"
EOF
  chmod +x "$fake_bin/docker" "$fake_bin/curl" "$fake_bin/cp" "$fake_bin/git" "$source/deploy/build-and-upgrade.sh"

}

run_upgrade() {
  local source="$1"
  local install="$2"
  local fake_bin="$3"
  local state="$4"
  shift 4
  PATH="$fake_bin:$PATH" \
    FAKE_STATE_DIR="$state" \
    INSTALL_DIR="$install" \
    HEALTH_TIMEOUT_SECONDS=1 \
    "$source/deploy/build-and-upgrade.sh" --install-dir "$install" --yes "$@"
}

run_upgrade_with_confirmation() {
  local source="$1"
  local install="$2"
  local fake_bin="$3"
  local state="$4"
  printf 'y\n' | PATH="$fake_bin:$PATH" \
    FAKE_STATE_DIR="$state" \
    INSTALL_DIR="$install" \
    HEALTH_TIMEOUT_SECONDS=1 \
    "$source/deploy/build-and-upgrade.sh" --install-dir "$install"
}

case "${1:-all}" in
  dry-run)
    make_fixture
    run_upgrade "$FIXTURE_SOURCE" "$FIXTURE_INSTALL" "$FIXTURE_FAKE_BIN" "$FIXTURE_STATE" --dry-run
    ;;
  all)
    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    original_env="$WORK_DIR/original.env"
    original_db="$WORK_DIR/original.db"
    cp "$install/.env" "$original_env"
    cp "$install/data/chatgpt2api.db" "$original_db"
    run_upgrade_with_confirmation "$source" "$install" "$fake_bin" "$state"
    build_line="$(grep -n -F 'docker build' "$state/commands.log" | head -n 1 | cut -d: -f1)"
    stop_line="$(grep -n -F 'docker compose' "$state/commands.log" | grep -F 'stop' | head -n 1 | cut -d: -f1)"
    run_line="$(grep -n -F 'docker run' "$state/commands.log" | head -n 1 | cut -d: -f1)"
    (( build_line < stop_line && stop_line < run_line )) || fail 'build/stop/marker order is invalid'
    grep -F 'docker compose' "$state/commands.log" | grep -F 'up' >/dev/null || fail 'compose up was not recorded'
    ! grep -F 'docker compose' "$state/commands.log" | grep -F 'pull' >/dev/null || fail 'application image pull was recorded'
    assert_file_contains "$install/.env" 'CHATGPT2API_IMAGE=chatgpt2api:branch-'
    assert_file_equals "$install/data/chatgpt2api.db" "$original_db"
    backup_db="$(find "$install/backups" -name chatgpt2api.db -type f | head -n 1)"
    [[ -n "$backup_db" ]] || fail 'backup database was not created'
    assert_file_equals "$backup_db" "$original_db"
    backup_dir="$(dirname "$backup_db")"
    assert_file_contains "$backup_dir/chatgpt2api.db-wal" 'original-wal'
    assert_file_contains "$backup_dir/ROLLBACK_IMAGE" 'chatgpt2api:rollback-'
    ! grep -F 'docker build' "$state/commands.log" | grep -F -- '--pull' >/dev/null || fail 'build must not force --pull'
    grep -F 'safe.directory=' "$state/commands.log" >/dev/null || fail 'git must run with safe.directory for sudo use'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/.env" "$WORK_DIR/before.env"
    cp "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    if FAKE_BUILD_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state"; then fail 'build failure unexpectedly succeeded'; fi
    assert_file_equals "$install/.env" "$WORK_DIR/before.env"
    assert_file_equals "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    ! grep -F 'docker compose' "$state/commands.log" | grep -E 'stop|up' >/dev/null || fail 'build failure mutated app'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/.env" "$WORK_DIR/before.env"
    cp "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    if FAKE_HEALTH_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state"; then fail 'health failure unexpectedly succeeded'; fi
    grep -v '^CHATGPT2API_IMAGE=' "$install/.env" >"$WORK_DIR/after.env.rest"
    grep -v '^CHATGPT2API_IMAGE=' "$WORK_DIR/before.env" >"$WORK_DIR/before.env.rest"
    assert_file_equals "$WORK_DIR/after.env.rest" "$WORK_DIR/before.env.rest"
    assert_file_contains "$install/.env" 'CHATGPT2API_IMAGE=chatgpt2api:rollback-'
    assert_file_equals "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    assert_file_contains "$install/data/chatgpt2api.db-wal" 'original-wal'
    [[ ! -e "$install/data/chatgpt2api.db-shm" ]] || fail 'rollback left the new SQLite SHM file in place'
    assert_file_contains "$state/commands.log" 'docker compose'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    if FAKE_HEALTH_FAIL_ALWAYS=1 run_upgrade "$source" "$install" "$fake_bin" "$state" 2>"$WORK_DIR/rollback-failed.err"; then
      fail 'permanent health failure unexpectedly succeeded'
    fi
    assert_file_contains "$WORK_DIR/rollback-failed.err" 'Rollback failed'
    assert_file_contains "$WORK_DIR/rollback-failed.err" '.chatgpt2api-image-version'
    assert_file_contains "$WORK_DIR/rollback-failed.err" 'chatgpt2api:rollback-'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    if FAKE_GIT_STATUS_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run 2>"$WORK_DIR/git-failed.err"; then
      fail 'git status failure unexpectedly passed preflight'
    fi
    assert_file_contains "$WORK_DIR/git-failed.err" 'ERROR: git status failed'
    ! grep -F 'docker build' "$state/commands.log" >/dev/null || fail 'git failure reached build'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/.env" "$WORK_DIR/before.env"
    cp "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    if FAKE_COPY_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state"; then fail 'backup failure unexpectedly succeeded'; fi
    assert_file_equals "$install/.env" "$WORK_DIR/before.env"
    assert_file_equals "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    assert_file_contains "$state/commands.log" 'docker compose'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/.env" "$WORK_DIR/before.env"
    cp "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    if FAKE_MARKER_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state"; then fail 'marker failure unexpectedly succeeded'; fi
    grep -v '^CHATGPT2API_IMAGE=' "$install/.env" >"$WORK_DIR/marker.after.env.rest"
    grep -v '^CHATGPT2API_IMAGE=' "$WORK_DIR/before.env" >"$WORK_DIR/marker.before.env.rest"
    assert_file_equals "$WORK_DIR/marker.after.env.rest" "$WORK_DIR/marker.before.env.rest"
    assert_file_contains "$install/.env" 'CHATGPT2API_IMAGE=chatgpt2api:rollback-'
    assert_file_equals "$install/data/chatgpt2api.db" "$WORK_DIR/before.db"
    [[ "$(tr -d '\n' <"$state/app_state")" == running ]] || fail 'marker failure did not restore running app'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/.env" "$WORK_DIR/stop.before.env"
    if FAKE_STOP_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state"; then fail 'stop failure unexpectedly succeeded'; fi
    assert_file_equals "$install/.env" "$WORK_DIR/stop.before.env"
    [[ "$(tr -d '\n' <"$state/app_state")" == running ]] || fail 'stop failure did not restore running app'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/.env" "$WORK_DIR/compose.before.env"
    cp "$install/data/chatgpt2api.db" "$WORK_DIR/compose.before.db"
    if FAKE_COMPOSE_FAIL=1 run_upgrade "$source" "$install" "$fake_bin" "$state"; then fail 'compose failure unexpectedly succeeded'; fi
    grep -v '^CHATGPT2API_IMAGE=' "$install/.env" >"$WORK_DIR/compose.after.env.rest"
    grep -v '^CHATGPT2API_IMAGE=' "$WORK_DIR/compose.before.env" >"$WORK_DIR/compose.before.env.rest"
    assert_file_equals "$WORK_DIR/compose.after.env.rest" "$WORK_DIR/compose.before.env.rest"
    assert_file_contains "$install/.env" 'CHATGPT2API_IMAGE=chatgpt2api:rollback-'
    assert_file_equals "$install/data/chatgpt2api.db" "$WORK_DIR/compose.before.db"
    [[ "$(tr -d '\n' <"$state/app_state")" == running ]] || fail 'compose failure did not restore running app'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    cp "$install/data/chatgpt2api.db" "$WORK_DIR/signal.before.db"
    if FAKE_SIGNAL_ON_UP=1 run_upgrade "$source" "$install" "$fake_bin" "$state" 2>"$WORK_DIR/signal.err"; then
      fail 'interrupted upgrade unexpectedly succeeded'
    fi
    assert_file_contains "$WORK_DIR/signal.err" 'Interrupted while the app was being replaced'
    assert_file_contains "$install/.env" 'CHATGPT2API_IMAGE=chatgpt2api:rollback-'
    assert_file_equals "$install/data/chatgpt2api.db" "$WORK_DIR/signal.before.db"
    [[ ! -e "$install/data/chatgpt2api.db-shm" ]] || fail 'interrupted upgrade left the new SQLite SHM file in place'
    [[ "$(tr -d '\n' <"$state/app_state")" == running ]] || fail 'interrupted upgrade did not restore running app'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    printf '3.2.4\n' >"$state/app_version"
    if run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run 2>"$WORK_DIR/online-update.err"; then
      fail 'online-updated runtime unexpectedly passed preflight'
    fi
    assert_file_contains "$WORK_DIR/online-update.err" 'online update'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    printf '3.2.2\n' >"$state/app_version"
    printf '3.2.2\n' >"$state/seed_version"
    if run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run 2>"$WORK_DIR/version.err"; then
      fail 'version change unexpectedly passed preflight'
    fi
    assert_file_contains "$WORK_DIR/version.err" 'differs from source VERSION'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    printf 'dirty\n' >>"$source/README"
    if run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run; then fail 'dirty source unexpectedly passed preflight'; fi
    ! grep -F 'docker build' "$state/commands.log" >/dev/null || fail 'dirty source reached build'

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    sed -i 's/^DATABASE_MODE=.*/DATABASE_MODE=postgres-local/' "$install/.env"
    if run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run; then fail 'postgres deployment unexpectedly passed preflight'; fi

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    rm "$install/data/chatgpt2api.db"
    if run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run; then fail 'missing database unexpectedly passed preflight'; fi

    make_fixture
    source="$FIXTURE_SOURCE"; install="$FIXTURE_INSTALL"; fake_bin="$FIXTURE_FAKE_BIN"; state="$FIXTURE_STATE"
    printf 'stopped\n' >"$state/app_state"
    if run_upgrade "$source" "$install" "$fake_bin" "$state" --dry-run; then fail 'stopped app unexpectedly passed preflight'; fi

    assert_file_contains "$ROOT_DIR/docs/deployment.md" 'build-and-upgrade.sh'
    assert_file_contains "$ROOT_DIR/docs/deployment.md" 'data/chatgpt2api.db'
    assert_file_contains "$ROOT_DIR/docs/deployment.md" '不会 fetch/checkout 源码'
    assert_file_contains "$ROOT_DIR/docs/deployment.md" '.chatgpt2api-image-version'
    assert_file_contains "$ROOT_DIR/docs/deployment.md" 'ROLLBACK_IMAGE'
    assert_file_contains "$ROOT_DIR/docs/deployment.md" 'chatgpt2api.db-wal'
    ;;
  *)
    fail "unknown test case: $1"
    ;;
esac

printf 'PASS: build-and-upgrade command-stub tests\n'
