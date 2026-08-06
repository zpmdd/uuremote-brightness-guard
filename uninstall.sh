#!/bin/zsh

emulate -L zsh
setopt ERR_EXIT NO_UNSET PIPE_FAIL

readonly LABEL="io.github.zpmdd.uuremote-brightness-guard"
readonly DOMAIN="gui/$(/usr/bin/id -u)"
readonly INSTALL_ROOT="${HOME}/Library/Application Support/UURemoteBrightnessGuard"
readonly RUNTIME_DIR="${INSTALL_ROOT}/runtime"
readonly STATE_DIR="${INSTALL_ROOT}/state"
readonly LOG_DIR="${HOME}/Library/Logs/UURemoteBrightnessGuard"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly PURGE_DATA="${1:-}"

/bin/launchctl bootout "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
/bin/sleep 1

if [[ -f "${STATE_DIR}/guard-state.json" || -f "${STATE_DIR}/brightness-snapshot.json" ]]; then
  if ! UURBG_STATE_DIR="${STATE_DIR}" \
    UURBG_HELPER_PATH="${RUNTIME_DIR}/DisplayBrightnessTool" \
    UURBG_FALLBACK="0.85" \
    UURBG_DDC_FALLBACK="0.70" \
    /usr/bin/python3 "${RUNTIME_DIR}/uuremote_brightness_guard.py" --restore-now; then
    print -u2 -- "Brightness could not be restored, so uninstall was cancelled."
    if [[ -f "${TARGET_PLIST}" ]]; then
      /bin/launchctl bootstrap "${DOMAIN}" "${TARGET_PLIST}" >/dev/null 2>&1 || true
      /bin/launchctl kickstart -k "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
    fi
    exit 1
  fi
fi

/bin/rm -f -- "${TARGET_PLIST}"
if [[ "${INSTALL_ROOT}" == "${HOME}/Library/Application Support/UURemoteBrightnessGuard" ]]; then
  /bin/rm -rf -- "${INSTALL_ROOT}"
fi

if [[ "${PURGE_DATA}" == "--purge" && "${LOG_DIR}" == "${HOME}/Library/Logs/UURemoteBrightnessGuard" ]]; then
  /bin/rm -rf -- "${LOG_DIR}"
  print -- "UU Remote Brightness Guard and its logs were removed."
else
  print -- "UU Remote Brightness Guard was removed. Logs were kept at: ${LOG_DIR}"
fi
