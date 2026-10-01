#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/chatgpt2api}"
DRY_RUN=0
ASSUME_YES=0
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"
APP_CONTAINER="chatgpt2api"
COMPOSE_FILE=""
ENV_FILE=""
CONFIG_FILE=""
DB_FILE=""
RUNTIME_VOLUME=""
OLD_IMAGE_ID=""
ROLLBACK_IMAGE_TAG=""
NEW_IMAGE_TAG=""
BACKUP_DIR=""
BACKUP_COMPLETE=0
ROLLBACK_STARTED=0
SOURCE_COMMIT=""
CHATGPT2API_PORT=""

usage() {
  cat <<'EOF'
Usage: deploy/build-and-upgrade.sh [options]

Build the current clean source checkout locally and upgrade an existing
installer-managed SQLite Docker Compose deployment.

Options:
  --install-dir PATH  Existing Compose installation (default: /opt/chatgpt2api)
  --dry-run           Validate and print the plan without building or changing services
  --yes               Skip the interactive confirmation
  -h, --help          Show this help

The script never fetches or checks out source and never pushes or pulls the
application image. The local build uses cached base images and contacts a
base-image registry only when a required layer is missing.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  return 1
}

die() {
  fail "$1"
  exit 1
}

parse_args() {
  while (($#)); do
    case "$1" in
      --install-dir)
        (($# >= 2)) || die '--install-dir requires a path'
        INSTALL_DIR="$2"
        shift 2
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --yes)
        ASSUME_YES=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

read_env_value() {
  local key="$1"
  awk -v key="$key" '
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub("^[[:space:]]*" key "[[:space:]]*=", "")
      sub("[[:space:]]+#.*$", "")
      print
      exit
    }
  ' "$ENV_FILE"
}

set_image_in_env() {
  local env_file="$1"
  local image="$2"
  python3 - "$env_file" "$image" <<'PY'
import os
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
image = sys.argv[2]
raw = path.read_text(encoding="utf-8")
lines = raw.splitlines(keepends=True)
indices = [
    i for i, line in enumerate(lines)
    if line.lstrip().startswith("CHATGPT2API_IMAGE=")
]
if len(indices) > 1:
    raise SystemExit("duplicate CHATGPT2API_IMAGE entries")
newline = "\n" if raw.endswith("\n") or not lines else ""
replacement = f"CHATGPT2API_IMAGE={image}\n"
if indices:
    index = indices[0]
    old = lines[index]
    replacement = replacement[:-1] + ("\n" if old.endswith("\n") else "")
    lines[index] = replacement
else:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(replacement)
mode = stat.S_IMODE(path.stat().st_mode)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
os.close(fd)
try:
    temporary_path = Path(temporary)
    temporary_path.write_text("".join(lines), encoding="utf-8")
    os.chmod(temporary_path, mode)
    os.replace(temporary_path, path)
finally:
    try:
        Path(temporary).unlink()
    except FileNotFoundError:
        pass
PY
}

source_git() {
  # The operator may run this with sudo on a checkout owned by another user.
  git -c safe.directory="$SOURCE_DIR" -C "$SOURCE_DIR" "$@"
}

marker_clear_command() {
  printf 'docker run --rm -v %q:/app --entrypoint python %q -c %q' \
    "$RUNTIME_VOLUME" "$1" \
    'from pathlib import Path; Path("/app/.chatgpt2api-image-version").unlink(missing_ok=True)'
}

compose() {
  (cd "$INSTALL_DIR" && docker compose -f "$COMPOSE_FILE" "$@")
}

clear_runtime_marker() {
  local image="$1"
  docker run --rm \
    -v "${RUNTIME_VOLUME}:/app" \
    --entrypoint python \
    "$image" \
    -c 'from pathlib import Path; Path("/app/.chatgpt2api-image-version").unlink(missing_ok=True)'
}

health_check() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    if curl --silent --show-error --fail --max-time 5 "http://127.0.0.1:${CHATGPT2API_PORT}/" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback() {
  local rollback_ok=1
  if ((ROLLBACK_STARTED)); then
    return 1
  fi
  ROLLBACK_STARTED=1
  set +e
  compose stop app >/dev/null 2>&1 || rollback_ok=0
  if ((BACKUP_COMPLETE)); then
    restore_database || rollback_ok=0
    cp "$BACKUP_DIR/config.json" "$CONFIG_FILE" || rollback_ok=0
    if ! cp "$BACKUP_DIR/.env" "$ENV_FILE"; then
      rollback_ok=0
    elif ! set_image_in_env "$ENV_FILE" "$ROLLBACK_IMAGE_TAG"; then
      rollback_ok=0
    fi
  fi
  clear_runtime_marker "$ROLLBACK_IMAGE_TAG" >/dev/null 2>&1 || rollback_ok=0
  compose up -d --no-build --force-recreate app >/dev/null 2>&1 || rollback_ok=0
  if ! health_check; then
    rollback_ok=0
  fi
  set -e
  if ((rollback_ok)); then
    printf 'Rollback restored the previous application image and service.\n' >&2
  else
    printf 'Rollback failed. Recover manually:\n' >&2
    printf '  1. cd %q && docker compose -f docker-compose.yml stop app\n' "$INSTALL_DIR" >&2
    printf '  2. Restore chatgpt2api.db (and any -wal/-shm, deleting the current ones), .env and config.json from %s\n' "$BACKUP_DIR" >&2
    printf '  3. Set CHATGPT2API_IMAGE=%s in .env (also recorded in %s/ROLLBACK_IMAGE)\n' "$ROLLBACK_IMAGE_TAG" "$BACKUP_DIR" >&2
    printf '  4. Clear the runtime marker so the old image reseeds /app:\n     %s\n' "$(marker_clear_command "$ROLLBACK_IMAGE_TAG")" >&2
    printf '  5. docker compose -f docker-compose.yml up -d --no-build --force-recreate app\n' >&2
  fi
  return $((rollback_ok == 1 ? 0 : 1))
}

preflight() {
  command_exists git || die 'git is required'
  command_exists docker || die 'docker is required'
  command_exists python3 || die 'python3 is required'
  command_exists curl || die 'curl is required'
  docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is required'
  [[ -d "$SOURCE_DIR/.git" ]] || die "source is not a Git checkout: $SOURCE_DIR"
  local source_status
  source_status="$(source_git status --porcelain)" || die "git status failed for $SOURCE_DIR"
  [[ -z "$source_status" ]] || die 'source checkout has uncommitted changes'
  SOURCE_COMMIT="$(source_git rev-parse HEAD)" || die "git rev-parse failed for $SOURCE_DIR"

  [[ -d "$INSTALL_DIR" ]] || die "install directory does not exist: $INSTALL_DIR"
  COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
  ENV_FILE="$INSTALL_DIR/.env"
  CONFIG_FILE="$INSTALL_DIR/config.json"
  DB_FILE="$INSTALL_DIR/data/chatgpt2api.db"
  [[ -f "$COMPOSE_FILE" ]] || die 'standard docker-compose.yml is missing'
  [[ ! -e "$INSTALL_DIR/docker-compose.postgres.yml" ]] || die 'PostgreSQL Compose overlay is not supported by this script'
  [[ -f "$ENV_FILE" ]] || die '.env is missing'
  [[ -f "$CONFIG_FILE" ]] || die 'config.json is missing'
  [[ -f "$DB_FILE" ]] || die 'default SQLite database data/chatgpt2api.db is missing'
  grep -F './data:/app/data' "$COMPOSE_FILE" >/dev/null || die 'Compose data mount is not the standard ./data mount'
  grep -F './config.json:/app/config.json' "$COMPOSE_FILE" >/dev/null || die 'Compose config mount is not the standard config.json mount'

  local database_mode database_url
  database_mode="$(read_env_value DATABASE_MODE)"
  database_url="$(read_env_value DATABASE_URL)"
  [[ -z "$database_mode" || "$database_mode" == sqlite ]] || die 'only DATABASE_MODE=sqlite is supported'
  [[ -z "$database_url" ]] || die 'nonempty DATABASE_URL is not supported by this script'
  CHATGPT2API_PORT="$(read_env_value CHATGPT2API_PORT)"
  [[ "$CHATGPT2API_PORT" =~ ^[0-9]+$ ]] || die 'CHATGPT2API_PORT must be numeric'
  ((CHATGPT2API_PORT >= 1 && CHATGPT2API_PORT <= 65535)) || die 'CHATGPT2API_PORT is outside the valid range'
  [[ "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'HEALTH_TIMEOUT_SECONDS must be a positive integer'

  local status db_bytes available_kb
  status="$(docker inspect --format='{{.State.Status}}' "$APP_CONTAINER" 2>/dev/null)" || die 'chatgpt2api container is missing'
  [[ "$status" == running ]] || die 'chatgpt2api container is not running'
  OLD_IMAGE_ID="$(docker inspect --format='{{.Image}}' "$APP_CONTAINER")"
  [[ -n "$OLD_IMAGE_ID" ]] || die 'could not determine the current image ID'
  RUNTIME_VOLUME="$(docker inspect --format='{{range .Mounts}}{{if eq .Destination "/app"}}{{.Name}}{{end}}{{end}}' "$APP_CONTAINER")"
  [[ -n "$RUNTIME_VOLUME" ]] || die 'chatgpt2api /app runtime volume is missing'
  db_bytes="$(stat -c '%s' "$DB_FILE")"
  available_kb="$(df -Pk "$INSTALL_DIR" | awk 'NR == 2 {print $4}')"
  [[ "$available_kb" =~ ^[0-9]+$ ]] || die 'could not determine free disk space'
  ((available_kb * 1024 > db_bytes)) || die 'not enough free disk space for the SQLite backup'

  NEW_IMAGE_TAG="chatgpt2api:branch-${SOURCE_COMMIT}"
  ROLLBACK_IMAGE_TAG="chatgpt2api:rollback-$(date +%Y%m%d%H%M%S)-${SOURCE_COMMIT:0:12}"
  BACKUP_DIR="$INSTALL_DIR/backups/chatgpt2api-upgrade-$(date +%Y%m%d%H%M%S)-${SOURCE_COMMIT:0:12}"
}

print_summary() {
  printf 'Source commit: %s\n' "$SOURCE_COMMIT"
  printf 'Install directory: %s\n' "$INSTALL_DIR"
  printf 'Local image: %s\n' "$NEW_IMAGE_TAG"
  printf 'Backup directory: %s\n' "$BACKUP_DIR"
  printf 'Rollback image tag: %s\n' "$ROLLBACK_IMAGE_TAG"
  printf 'Health URL: http://127.0.0.1:%s/\n' "$CHATGPT2API_PORT"
}

confirm_upgrade() {
  ((ASSUME_YES)) && return 0
  printf 'Proceed with local build and application restart? [y/N] ' >&2
  local answer
  read -r answer || return 1
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

create_backup() {
  local suffix
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  printf '%s\n' "$ROLLBACK_IMAGE_TAG" >"$BACKUP_DIR/ROLLBACK_IMAGE" || return 1
  cp "$DB_FILE" "$BACKUP_DIR/chatgpt2api.db" || return 1
  # SQLite runs in WAL mode: committed rows may still live in the -wal file.
  for suffix in -wal -shm; do
    if [[ -e "${DB_FILE}${suffix}" ]]; then
      cp "${DB_FILE}${suffix}" "$BACKUP_DIR/chatgpt2api.db${suffix}" || return 1
    fi
  done
  cp "$ENV_FILE" "$BACKUP_DIR/.env" || return 1
  cp "$CONFIG_FILE" "$BACKUP_DIR/config.json" || return 1
  chmod 600 "$BACKUP_DIR"/*
  chmod 600 "$BACKUP_DIR/.env"
  : >"$BACKUP_DIR/BACKUP_COMPLETE"
  BACKUP_COMPLETE=1
}

restore_database() {
  local suffix
  cp "$BACKUP_DIR/chatgpt2api.db" "$DB_FILE" || return 1
  for suffix in -wal -shm; do
    # Never leave the new version's WAL/SHM beside the restored database.
    rm -f "${DB_FILE}${suffix}" || return 1
    if [[ -e "$BACKUP_DIR/chatgpt2api.db${suffix}" ]]; then
      cp "$BACKUP_DIR/chatgpt2api.db${suffix}" "${DB_FILE}${suffix}" || return 1
    fi
  done
}

upgrade() {
  confirm_upgrade || die 'upgrade cancelled'
  docker build -t "$NEW_IMAGE_TAG" "$SOURCE_DIR" || die 'local Docker build failed; production was not changed'
  docker tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE_TAG" || die 'could not create rollback image tag'
  if ! compose stop app; then
    printf 'Could not stop the app service cleanly; attempting to restart the old application.\n' >&2
    rollback || true
    exit 1
  fi

  if ! create_backup; then
    printf 'Backup creation failed; attempting to restart the old application.\n' >&2
    rollback || true
    exit 1
  fi
  if ! set_image_in_env "$ENV_FILE" "$NEW_IMAGE_TAG"; then
    printf 'Image pin failed; attempting rollback.\n' >&2
    rollback || true
    exit 1
  fi
  if ! clear_runtime_marker "$NEW_IMAGE_TAG"; then
    printf 'Runtime marker refresh failed; attempting rollback.\n' >&2
    rollback || true
    exit 1
  fi
  if ! compose up -d --no-build --force-recreate app; then
    printf 'Application recreation failed; attempting rollback.\n' >&2
    rollback || true
    exit 1
  fi
  if ! health_check; then
    printf 'Application health check failed; attempting rollback.\n' >&2
    rollback || true
    exit 1
  fi
  printf 'Upgrade succeeded.\n'
  printf 'Source commit: %s\n' "$SOURCE_COMMIT"
  printf 'Image: %s\n' "$NEW_IMAGE_TAG"
  printf 'Backup: %s\n' "$BACKUP_DIR"
  printf 'Rollback image tag: %s\n' "$ROLLBACK_IMAGE_TAG"
}

main() {
  parse_args "$@"
  preflight
  print_summary
  if ((DRY_RUN)); then
    printf 'Dry run: no build or service mutation performed.\n'
    return 0
  fi
  upgrade
}

main "$@"
