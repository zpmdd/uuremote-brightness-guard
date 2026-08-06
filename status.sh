#!/bin/zsh

emulate -L zsh
setopt PIPE_FAIL

readonly LABEL="io.github.zpmdd.uuremote-brightness-guard"
readonly DOMAIN="gui/$(/usr/bin/id -u)"
readonly INSTALL_ROOT="${HOME}/Library/Application Support/UURemoteBrightnessGuard"
readonly RUNTIME_DIR="${INSTALL_ROOT}/runtime"
readonly STATE_DIR="${INSTALL_ROOT}/state"
readonly LOG_DIR="${HOME}/Library/Logs/UURemoteBrightnessGuard"

if [[ -x "${RUNTIME_DIR}/uuremote_brightness_guard.py" ]]; then
  UURBG_STATE_DIR="${STATE_DIR}" \
  UURBG_HELPER_PATH="${RUNTIME_DIR}/DisplayBrightnessTool" \
  /usr/bin/python3 "${RUNTIME_DIR}/uuremote_brightness_guard.py" --status
else
  print -- "Installation: not found"
fi

if /bin/launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  print -- "LaunchAgent: running"
else
  print -- "LaunchAgent: not running"
fi

if [[ -f "${LOG_DIR}/guard.log" ]]; then
  print -- ""
  print -- "Last 10 events:"
  /usr/bin/tail -n 10 "${LOG_DIR}/guard.log"
fi

if [[ -s "${LOG_DIR}/guard.error.log" ]]; then
  print -- ""
  print -- "Recent errors:"
  /usr/bin/tail -n 10 "${LOG_DIR}/guard.error.log"
fi
