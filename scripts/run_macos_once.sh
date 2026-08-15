#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${AI_DATA_EXTRACTION_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    for candidate in python3 python; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v "${candidate}")"
            break
        fi
    done
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "A working Python 3 executable is required. Set AI_DATA_EXTRACTION_PYTHON." >&2
    exit 1
fi

if [[ "${PYTHON_BIN}" != "${REPO_ROOT}/.venv/bin/python3" ]]; then
    "${PYTHON_BIN}" -m venv "${REPO_ROOT}/.venv"
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python3"
fi

if ! "${PYTHON_BIN}" -c 'import boto3' >/dev/null 2>&1; then
    "${PYTHON_BIN}" -m pip install -r "${REPO_ROOT}/requirements.txt"
fi

if [[ -z "${AI_DATA_EXTRACTION_HOST_ID:-}" ]]; then
    if command -v scutil >/dev/null 2>&1; then
        mac_name="$(scutil --get LocalHostName 2>/dev/null || hostname)"
    else
        mac_name="$(hostname)"
    fi
    export AI_DATA_EXTRACTION_HOST_ID="macos-${mac_name}"
fi
export AI_DATA_EXTRACTION_DATA_DIR="${AI_DATA_EXTRACTION_DATA_DIR:-${HOME}/.local/share/ai-data-extraction}"

repo_config="${REPO_ROOT}/.config/ai-data-extraction/config.json"
if [[ -z "${AI_DATA_EXTRACTION_CONFIG:-}" && -f "${repo_config}" ]]; then
    export AI_DATA_EXTRACTION_CONFIG="${repo_config}"
fi

echo "Running one manual macOS extraction for ${AI_DATA_EXTRACTION_HOST_ID}."
exec "${REPO_ROOT}/scripts/run_backup_once.sh" "$@"
