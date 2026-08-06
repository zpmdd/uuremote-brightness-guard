#!/bin/zsh

emulate -L zsh
setopt ERR_EXIT NO_UNSET PIPE_FAIL

readonly PROJECT_DIR="${0:A:h:h}"
readonly OUTPUT_DIR="${PROJECT_DIR}/.build/plist-test"
readonly OUTPUT_PLIST="${OUTPUT_DIR}/agent.plist"
readonly RUNTIME_DIR="/tmp/UU Remote Guard Test/runtime"
readonly STATE_DIR="/tmp/UU Remote Guard Test/state"

/bin/mkdir -p "${OUTPUT_DIR}"
"${PROJECT_DIR}/scripts/render_launchagent.sh" \
  "${PROJECT_DIR}/launchd/io.github.zpmdd.uuremote-brightness-guard.plist" \
  "${OUTPUT_PLIST}" \
  "${RUNTIME_DIR}" \
  "${STATE_DIR}" \
  "/tmp/UU Remote Guard Test/guard.log" \
  "/tmp/UU Remote Guard Test/guard.error.log"

[[ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "${OUTPUT_PLIST}")" == "/usr/bin/python3" ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "${OUTPUT_PLIST}")" == "${RUNTIME_DIR}/uuremote_brightness_guard.py" ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:UURBG_STATE_DIR' "${OUTPUT_PLIST}")" == "${STATE_DIR}" ]]

print -- "LaunchAgent render test passed."
