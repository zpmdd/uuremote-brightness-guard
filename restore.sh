#!/bin/zsh

emulate -L zsh
setopt ERR_EXIT NO_UNSET PIPE_FAIL

readonly LABEL="io.github.zpmdd.uuremote-brightness-guard"
readonly DOMAIN="gui/$(/usr/bin/id -u)"
readonly INSTALL_ROOT="${HOME}/Library/Application Support/UURemoteBrightnessGuard"
readonly RUNTIME_DIR="${INSTALL_ROOT}/runtime"
readonly STATE_DIR="${INSTALL_ROOT}/state"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -x "${RUNTIME_DIR}/uuremote_brightness_guard.py" || ! -x "${RUNTIME_DIR}/DisplayBrightnessTool" ]]; then
  print -u2 -- "The guard is not installed."
  exit 1
fi

typeset HAD_SNAPSHOT="false"
if [[ -f "${STATE_DIR}/guard-state.json" || -f "${STATE_DIR}/brightness-snapshot.json" ]]; then
  HAD_SNAPSHOT="true"
fi
/bin/launchctl bootout "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
/bin/sleep 1

if [[ "${HAD_SNAPSHOT}" == "false" || -f "${STATE_DIR}/guard-state.json" || -f "${STATE_DIR}/brightness-snapshot.json" ]]; then
  UURBG_STATE_DIR="${STATE_DIR}" \
  UURBG_HELPER_PATH="${RUNTIME_DIR}/DisplayBrightnessTool" \
  UURBG_FALLBACK="0.85" \
  UURBG_DDC_FALLBACK="0.70" \
  /usr/bin/python3 "${RUNTIME_DIR}/uuremote_brightness_guard.py" --restore-now
fi

if [[ -f "${TARGET_PLIST}" ]]; then
  /bin/launchctl bootstrap "${DOMAIN}" "${TARGET_PLIST}"
  /bin/launchctl enable "${DOMAIN}/${LABEL}"
  /bin/launchctl kickstart -k "${DOMAIN}/${LABEL}"
fi

print -- "Brightness restore completed. The guard remains enabled."
