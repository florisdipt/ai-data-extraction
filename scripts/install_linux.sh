#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${AI_DATA_EXTRACTION_PYTHON:-python3}"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/ai-data-extraction"
DATA_DIR="${AI_DATA_EXTRACTION_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/ai-data-extraction}"
STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
STATE_DIR="${AI_DATA_EXTRACTION_STATE_DIR:-${STATE_HOME}/ai-data-extraction}"

"${PYTHON_BIN}" -m venv "${REPO_ROOT}/.venv"
"${REPO_ROOT}/.venv/bin/python3" -m pip install --upgrade pip
"${REPO_ROOT}/.venv/bin/python3" -m pip install -r "${REPO_ROOT}/requirements.txt"
mkdir -p "${CONFIG_DIR}" "${DATA_DIR}" "${STATE_DIR}"
chmod 700 "${CONFIG_DIR}" "${DATA_DIR}" "${STATE_DIR}"

if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
    cat >&2 <<EOF
Create ${CONFIG_DIR}/config.json before the first upload.
Use mode 0600 and keep the access key outside Git.
EOF
fi

if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
    if [[ "${EUID}" -eq 0 ]]; then
        unit_dir="/etc/systemd/system"
        systemctl_command=(systemctl)
    elif systemctl --user show-environment >/dev/null 2>&1; then
        unit_dir="${HOME}/.config/systemd/user"
        systemctl_command=(systemctl --user)
    else
        unit_dir=""
        systemctl_command=()
    fi

    if [[ -n "${unit_dir}" ]]; then
        mkdir -p "${unit_dir}"
        sed \
            -e "s|@REPO_ROOT@|${REPO_ROOT}|g" \
            -e "s|@CONFIG_DIR@|${CONFIG_DIR}|g" \
            -e "s|@DATA_DIR@|${DATA_DIR}|g" \
            -e "s|@STATE_DIR@|${STATE_DIR}|g" \
            "${REPO_ROOT}/systemd/ai-data-extraction.service" \
            > "${unit_dir}/ai-data-extraction.service"
        install -m 0644 "${REPO_ROOT}/systemd/ai-data-extraction.timer" \
            "${unit_dir}/ai-data-extraction.timer"
        "${systemctl_command[@]}" daemon-reload
        "${systemctl_command[@]}" enable ai-data-extraction.timer
        if [[ -f "${CONFIG_DIR}/config.json" ]]; then
            "${systemctl_command[@]}" start ai-data-extraction.timer
            echo "Installed and started the hourly systemd timer."
        else
            echo "Installed the hourly systemd timer. Add config.json, then start ai-data-extraction.timer."
        fi
        exit 0
    fi
fi

bashrc_hook="${CONFIG_DIR}/bashrc-hook.sh"
cat > "${bashrc_hook}" <<EOF
case \"\$-\" in
    *i*) ${REPO_ROOT}/scripts/start_linux_supervisor.sh >/dev/null 2>&1 || true ;;
esac
EOF
chmod 700 "${bashrc_hook}"

if [[ "${CONFIG_DIR}" == "${HOME}/.config/ai-data-extraction" ]]; then
    marker='source "$HOME/.config/ai-data-extraction/bashrc-hook.sh"'
else
    marker="source \"${CONFIG_DIR}/bashrc-hook.sh\""
fi
if ! grep -Fqx "${marker}" "${HOME}/.bashrc" 2>/dev/null; then
    printf '\n%s\n' "${marker}" >> "${HOME}/.bashrc"
fi
echo "Installed the guarded Linux shell supervisor hook. Open a new shell to start it."
