#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
STATE_DIR="${AI_DATA_EXTRACTION_STATE_DIR:-${STATE_HOME}/ai-data-extraction}"
LOG_FILE="${STATE_DIR}/supervisor.log"

mkdir -p "${STATE_DIR}"
exec 8>"${STATE_DIR}/supervisor.lock"
if ! flock -n 8; then
    exit 0
fi
exec 9>"${STATE_DIR}/run.lock"

trap 'rm -f "${STATE_DIR}/scheduler.pid"' EXIT
printf '%s\n' "$$" > "${STATE_DIR}/scheduler.pid"

while true; do
    now="$(date -u +%s)"
    next_hour=$(( (now / 3600 + 1) * 3600 ))
    sleep_seconds=$(( next_hour - now ))
    sleep "${sleep_seconds}"

    if flock -n 9; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup start" >> "${LOG_FILE}"
        if "${REPO_ROOT}/scripts/run_backup_once.sh" >> "${LOG_FILE}" 2>&1; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup complete" >> "${LOG_FILE}"
        else
            status=$?
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup failed with ${status}" >> "${LOG_FILE}"
        fi
        flock -u 9
    fi
done
