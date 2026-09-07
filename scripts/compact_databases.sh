#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$ROOT/data/db-backups"
LOCK="$ROOT/data/db-compaction.lock"
LOG="$ROOT/data/db-compaction.log"
STOP_TIMEOUT="${DB_COMPACTION_STOP_TIMEOUT_SECONDS:-120}"
MIN_FREE_GB="${DB_COMPACTION_MIN_FREE_GB:-20}"
MAKE_BACKUP="${DB_COMPACTION_BACKUP:-false}"

usage() {
  printf '%s\n' \
    'Usage: scripts/compact_databases.sh' \
    '' \
    'Compacts the configured market, analyst, and regime databases while' \
    'stopping and restarting their managed writers safely.' \
    '' \
    'Configuration is supplied through DB_COMPACTION_* and database path' \
    'environment variables. Use DB_COMPACTION_ONLY=market|analyst|regime' \
    'to limit the operation.'
}

case "${1:-}" in
  "") ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    printf 'unknown argument: %s\n\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac

load_dotenv_paths() {
  local python="$ROOT/venv/bin/python"
  [[ -x "$python" ]] || python=python3
  [[ -f "$ROOT/.env" ]] || return 0
  if ! "$python" -c 'import dotenv' >/dev/null 2>&1; then
    printf 'refusing compaction: cannot parse %s/.env\n' "$ROOT" >&2
    return 1
  fi
  local values
  values="$("$python" - "$ROOT/.env" <<'PY'
from dotenv import dotenv_values
import sys

values = dotenv_values(sys.argv[1])
for key in ("MARKET_DB_PATH", "ANALYST_DB_PATH", "REGIME_DB_PATH"):
    value = values.get(key)
    if value is not None:
        print(f"{key}\t{value}")
PY
  )" || return 1
  while IFS=$'\t' read -r key value; do
    case "$key" in
      MARKET_DB_PATH) [[ -v MARKET_DB_PATH ]] || MARKET_DB_PATH="$value" ;;
      ANALYST_DB_PATH) [[ -v ANALYST_DB_PATH ]] || ANALYST_DB_PATH="$value" ;;
      REGIME_DB_PATH) [[ -v REGIME_DB_PATH ]] || REGIME_DB_PATH="$value" ;;
    esac
  done <<< "$values"
}

load_dotenv_paths

resolve_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$ROOT" "$1" ;;
  esac
}

MARKET_DB="$(resolve_path "${MARKET_DB_PATH:-data/market.sqlite3}")"
ANALYST_DB="$(resolve_path "${ANALYST_DB_PATH:-data/analyst.sqlite3}")"
REGIME_DB="$(resolve_path "${REGIME_DB_PATH:-data/regime.sqlite3}")"

SERVICES=(
  research-analyst-orchestrator
  research-analyst-regime-session
  research-analyst-ws
  research-analyst-symbol-rotation
)
REQUIRED_SERVICES=(
  research-analyst-orchestrator
  research-analyst-regime-session
  research-analyst-ws
)
DB_LABELS=(market analyst regime)
DB_PATHS=("$MARKET_DB" "$ANALYST_DB" "$REGIME_DB")
PRUNER_NAMES=(market analyst regime)
case "${DB_COMPACTION_ONLY:-all}" in
  market)
    DB_LABELS=(market); DB_PATHS=("$MARKET_DB"); PRUNER_NAMES=(market) ;;
  analyst)
    DB_LABELS=(analyst); DB_PATHS=("$ANALYST_DB"); PRUNER_NAMES=(analyst) ;;
  regime)
    DB_LABELS=(regime); DB_PATHS=("$REGIME_DB"); PRUNER_NAMES=(regime) ;;
  all) ;;
  *)
    printf 'refusing compaction: DB_COMPACTION_ONLY must be all, market, analyst, or regime\n' >&2
    exit 1
    ;;
esac
declare -A WAS_ACTIVE
declare -a MANAGED_SERVICES

mkdir -p "$BACKUP_DIR"
exec 9>"$LOCK"
flock -n 9 || {
  printf '[%s] compaction already running\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >>"$LOG"
  exit 0
}
exec >>"$LOG" 2>&1

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

compact_regime_provenance() {
  [[ " ${DB_LABELS[*]} " == *" regime "* ]] || return 0
  log "bounding persisted regime provenance"
  export PYTHONPATH="$ROOT/src/research_analyst"
  local python_bin="$ROOT/venv/bin/python"
  [[ -x "$python_bin" ]] || python_bin=python3
  "$python_bin" - "$REGIME_DB" <<'PY'
import json
import sqlite3
import sys

from config import REGIME_PROVENANCE_MAX_IDS

path = sys.argv[1]
limit = int(REGIME_PROVENANCE_MAX_IDS)
connection = sqlite3.connect(path, timeout=30.0)
try:
    rows = connection.execute(
        "SELECT rowid, source_observation_ids, source_references_json "
        "FROM regime_scores"
    )
    updates = []
    changed = 0
    for rowid, observation_text, references_text in rows:
        try:
            observation_ids = json.loads(observation_text or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            observation_ids = []
        try:
            references = json.loads(references_text or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            references = {}
        if not isinstance(observation_ids, list):
            observation_ids = []
        if not isinstance(references, dict):
            references = {}
        bounded_observations = observation_ids[-limit:]
        bounded_references = dict(references)
        for key, value in references.items():
            if isinstance(value, list):
                bounded_references[key] = value[-limit:]
        new_observation_text = json.dumps(bounded_observations, sort_keys=True)
        new_references_text = json.dumps(bounded_references, sort_keys=True)
        if new_observation_text == observation_text and new_references_text == references_text:
            continue
        updates.append((new_observation_text, new_references_text, rowid))
        if len(updates) >= 250:
            connection.executemany(
                "UPDATE regime_scores SET source_observation_ids=?, "
                "source_references_json=? WHERE rowid=?",
                updates,
            )
            connection.commit()
            changed += len(updates)
            updates.clear()
    if updates:
        connection.executemany(
            "UPDATE regime_scores SET source_observation_ids=?, "
            "source_references_json=? WHERE rowid=?",
            updates,
        )
        connection.commit()
        changed += len(updates)
    print(f"bounded regime provenance rows={changed} limit={limit}")
finally:
    connection.close()
PY
}

service_status() {
  local service="$1"
  oxmgr list --json | python3 -c '
import json
import sys

name = sys.argv[1]
services = json.load(sys.stdin)
match = next((item for item in services if item.get("name") == name), None)
print(match.get("status", "missing") if match else "missing")
' "$service"
}

for db in "${DB_PATHS[@]}"; do
  if [[ ! -f "$db" ]]; then
    log "refusing compaction: database not found: $db"
    exit 1
  fi
done

total_db_kb=0
largest_db_kb=0
for db in "${DB_PATHS[@]}"; do
  size_kb="$(du -Pk "$db" | awk 'NR == 1 {print $1}')"
  total_db_kb=$((total_db_kb + size_kb))
  ((size_kb > largest_db_kb)) && largest_db_kb="$size_kb"
done
required_free_kb=$((MIN_FREE_GB * 1024 * 1024 + largest_db_kb))
if [[ "$MAKE_BACKUP" == true ]]; then
  required_free_kb=$((required_free_kb + total_db_kb))
fi
for location in "$ROOT" "$BACKUP_DIR" "${DB_PATHS[@]}"; do
  free_kb="$(df -Pk "$location" | awk 'NR == 2 {print $4}')"
  if (( free_kb < required_free_kb )); then
    log "refusing compaction: insufficient free space on filesystem for $location"
    exit 1
  fi
done

for service in "${SERVICES[@]}"; do
  status="$(service_status "$service")"
  if [[ "$status" == "missing" ]]; then
    required=false
    for required_service in "${REQUIRED_SERVICES[@]}"; do
      [[ "$required_service" == "$service" ]] && required=true
    done
    if [[ "$required" == true ]]; then
      log "refusing compaction: managed service is not registered: $service"
      exit 1
    fi
    continue
  fi
  MANAGED_SERVICES+=("$service")
  WAS_ACTIVE["$service"]="$([[ "$status" != "stopped" ]] && printf true || printf false)"
done

restart_services() {
  local exit_code="$1"
  local index
  local restart_failed=false
  for service in "${MANAGED_SERVICES[@]}"; do
    if [[ "${WAS_ACTIVE[$service]}" == true ]]; then
      log "starting $service"
      if ! oxmgr restart "$service"; then
        log "restart failed: $service"
        restart_failed=true
      fi
    fi
  done
  for service in "${MANAGED_SERVICES[@]}"; do
    if [[ "${WAS_ACTIVE[$service]}" == true ]]; then
      for ((seconds = 0; seconds < STOP_TIMEOUT; seconds++)); do
        if [[ "$(service_status "$service")" == "running" ]]; then
          break
        fi
        sleep 1
      done
      if [[ "$(service_status "$service")" != "running" ]]; then
        log "restart did not become healthy: $service"
        restart_failed=true
      fi
    fi
  done
  if [[ "$restart_failed" == true ]]; then
    exit 1
  fi
  exit "$exit_code"
}
trap 'restart_services "$?"' EXIT

for service in "${MANAGED_SERVICES[@]}"; do
  if [[ "$(service_status "$service")" != "stopped" ]]; then
    log "stopping $service"
    oxmgr stop "$service"
  fi
done

for ((seconds = 0; seconds < STOP_TIMEOUT; seconds++)); do
  still_running=false
  for service in "${MANAGED_SERVICES[@]}"; do
    case "$(service_status "$service")" in
      running|starting|stopping) still_running=true ;;
    esac
  done
  [[ "$still_running" == false ]] && break
  sleep 1
done

for service in "${MANAGED_SERVICES[@]}"; do
  case "$(service_status "$service")" in
    running|starting|stopping)
      log "refusing compaction: $service did not stop within ${STOP_TIMEOUT}s"
      exit 1
      ;;
  esac
done

stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
if [[ "$MAKE_BACKUP" == true ]]; then
  for index in "${!DB_PATHS[@]}"; do
    db="${DB_PATHS[$index]}"
    label="${DB_LABELS[$index]}"
    backup="$BACKUP_DIR/${label}.sqlite3.$stamp"
    log "creating backup $backup"
    sqlite3 "$db" ".backup '$backup'"
  done
fi

log "deleting rows outside retention windows"
export PYTHONPATH="$ROOT/src/research_analyst"
python_bin="$ROOT/venv/bin/python"
[[ -x "$python_bin" ]] || python_bin=python3
compact_regime_provenance
"$python_bin" - "${PRUNER_NAMES[@]}" -- "${DB_PATHS[@]}" <<'PY'
import sqlite3
import sys

from db_maintenance import prune_analyst_db, prune_market_db, prune_regime_db

pruners = {"market": prune_market_db, "analyst": prune_analyst_db, "regime": prune_regime_db}
separator = sys.argv.index("--")
for name, path in zip(sys.argv[1:separator], sys.argv[separator + 1:]):
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        pruners[name](conn)
    finally:
        conn.close()
PY

for db in "${DB_PATHS[@]}"; do
  log "compacting $db"
  sqlite3 "$db" "PRAGMA journal_mode=WAL; PRAGMA wal_checkpoint(TRUNCATE); VACUUM; PRAGMA optimize;"
  [[ "$(sqlite3 -readonly "$db" 'PRAGMA integrity_check;')" == ok ]] || {
    log "integrity check failed: $db"
    exit 1
  }
done

for label in "${DB_LABELS[@]}"; do
  mapfile -t old_backups < <(ls -1t "$BACKUP_DIR/${label}.sqlite3."* 2>/dev/null | awk 'NR > 2')
  if ((${#old_backups[@]})); then
    rm -f -- "${old_backups[@]}"
  fi
done
log "database compaction completed successfully"
