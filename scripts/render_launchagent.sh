#!/bin/zsh

emulate -L zsh
setopt ERR_EXIT NO_UNSET PIPE_FAIL

if [[ "$#" -ne 6 ]]; then
  print -u2 -- "Usage: render_launchagent.sh TEMPLATE OUTPUT RUNTIME_DIR STATE_DIR STDOUT STDERR"
  exit 2
fi

readonly SOURCE_PLIST="$1"
readonly TARGET_PLIST="$2"
readonly RUNTIME_DIR="$3"
readonly STATE_DIR="$4"
readonly STDOUT_PATH="$5"
readonly STDERR_PATH="$6"

/usr/bin/install -m 0644 "${SOURCE_PLIST}" "${TARGET_PLIST}"
/usr/bin/plutil -insert ProgramArguments.1 \
  -string "${RUNTIME_DIR}/uuremote_brightness_guard.py" \
  "${TARGET_PLIST}"
/usr/bin/plutil -replace WorkingDirectory -string "${RUNTIME_DIR}" "${TARGET_PLIST}"
/usr/bin/plutil -replace EnvironmentVariables.UURBG_HELPER_PATH \
  -string "${RUNTIME_DIR}/DisplayBrightnessTool" \
  "${TARGET_PLIST}"
/usr/bin/plutil -replace EnvironmentVariables.UURBG_STATE_DIR \
  -string "${STATE_DIR}" \
  "${TARGET_PLIST}"
/usr/bin/plutil -replace StandardOutPath -string "${STDOUT_PATH}" "${TARGET_PLIST}"
/usr/bin/plutil -replace StandardErrorPath -string "${STDERR_PATH}" "${TARGET_PLIST}"
/usr/bin/plutil -lint "${TARGET_PLIST}" >/dev/null
