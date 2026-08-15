#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${AI_DATA_EXTRACTION_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "${AI_DATA_EXTRACTION_HOST_ID:-}" ]]; then
    if command -v scutil >/dev/null 2>&1; then
        mac_name="$(scutil --get LocalHostName 2>/dev/null || hostname)"
    else
        mac_name="$(hostname)"
    fi
    export AI_DATA_EXTRACTION_HOST_ID="macos-${mac_name}"
fi

echo "Running one manual macOS extraction for ${AI_DATA_EXTRACTION_HOST_ID}."
exec "${REPO_ROOT}/scripts/run_backup_once.sh" "$@"
