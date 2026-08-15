#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
STATE_DIR="${AI_DATA_EXTRACTION_STATE_DIR:-${STATE_HOME}/ai-data-extraction}"
PID_FILE="${STATE_DIR}/scheduler.pid"

mkdir -p "${STATE_DIR}"
exec 8>"${STATE_DIR}/starter.lock"
flock -n 8 || exit 0

if [[ -s "${PID_FILE}" ]]; then
    pid="$(cat "${PID_FILE}")"
    if kill -0 "${pid}" 2>/dev/null && [[ -r "/proc/${pid}/cmdline" ]]; then
        if tr '\0' ' ' < "/proc/${pid}/cmdline" | grep -Fq "ai_data_extraction_supervisor.sh"; then
            exit 0
        fi
    fi
fi

nohup "${REPO_ROOT}/scripts/ai_data_extraction_supervisor.sh" >/dev/null 2>&1 </dev/null &
echo $! > "${PID_FILE}"
