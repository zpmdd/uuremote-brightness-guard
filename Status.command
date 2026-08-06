#!/bin/zsh

emulate -L zsh
setopt NO_UNSET PIPE_FAIL

readonly SCRIPT_DIR="${0:A:h}"
clear
print -- "UU Remote Brightness Guard Status"
print -- ""
"${SCRIPT_DIR}/status.sh"
readonly RESULT=$?

if [[ -t 0 ]]; then
  print -- ""
  read -k 1 "?Press any key to close this window..."
  print -- ""
fi
exit "${RESULT}"
