#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${AI_DATA_EXTRACTION_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

if "${PYTHON_BIN}" "${REPO_ROOT}/extract_incremental.py" "$@"; then
    extract_status=0
else
    extract_status=$?
fi

if [[ "${extract_status}" -eq 75 ]]; then
    echo "extraction is already running. The upload step is skipped."
    exit 0
fi

# Upload successful extractor output even when one source returned a partial result.
"${PYTHON_BIN}" "${REPO_ROOT}/upload_all_to_s3_compatible.py"
exit "${extract_status}"
