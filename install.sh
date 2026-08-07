#!/bin/zsh

emulate -L zsh
setopt ERR_EXIT NO_UNSET PIPE_FAIL

readonly SCRIPT_DIR="${0:A:h}"
readonly LABEL="io.github.zpmdd.uuremote-brightness-guard"
readonly DOMAIN="gui/$(/usr/bin/id -u)"
readonly SOURCE_PLIST="${SCRIPT_DIR}/launchd/${LABEL}.plist"
readonly INSTALL_ROOT="${HOME}/Library/Application Support/UURemoteBrightnessGuard"
readonly RUNTIME_DIR="${INSTALL_ROOT}/runtime"
readonly STATE_DIR="${INSTALL_ROOT}/state"
readonly LOG_DIR="${HOME}/Library/Logs/UURemoteBrightnessGuard"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly BUILD_DIR="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/uurbg-install.XXXXXX")"

function cleanup_build_dir() {
  if [[ -n "${BUILD_DIR}" && -d "${BUILD_DIR}" ]]; then
    /bin/rm -rf -- "${BUILD_DIR}"
  fi
}
trap cleanup_build_dir EXIT

function wait_until_agent_is_unloaded() {
  local attempt
  for attempt in {1..50}; do
    if ! /bin/launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
      return 0
    fi
    /bin/sleep 0.1
  done
  return 1
}

function bootstrap_agent() {
  local plist_path="$1"
  local attempt
  for attempt in {1..3}; do
    if /bin/launchctl bootstrap "${DOMAIN}" "${plist_path}"; then
      return 0
    fi
    /bin/sleep 0.5
  done
  return 1
}

if [[ "$(/usr/bin/uname -m)" != "arm64" ]]; then
  print -u2 -- "This release currently supports Apple Silicon Macs only."
  exit 1
fi

if [[ ! -x /Applications/UURemote.app/Contents/Helpers/UURemoteServer ]]; then
  print -u2 -- "UU Remote was not found in /Applications. Install or move it there first."
  exit 1
fi

if [[ ! -x /Applications/MonitorControl.app/Contents/MacOS/MonitorControl ]]; then
  print -- "MonitorControl was not found. Continuing: it is optional for built-in-only Macs."
  print -- "For external displays, installing it is recommended for DDC/CI testing and manual recovery."
fi

if ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
  print -u2 -- "Xcode Command Line Tools are required. Run: xcode-select --install"
  exit 1
fi

/usr/bin/plutil -lint "${SOURCE_PLIST}" >/dev/null
PYTHONPYCACHEPREFIX="${BUILD_DIR}/pycache" \
  /usr/bin/python3 -m py_compile "${SCRIPT_DIR}/uuremote_brightness_guard.py"

print -- "Building the local display helper..."
/usr/bin/xcrun swiftc \
  -O \
  -sdk "$(/usr/bin/xcrun --sdk macosx --show-sdk-path)" \
  -import-objc-header "${SCRIPT_DIR}/src/Bridging-Header.h" \
  -framework AppKit \
  -framework CoreGraphics \
  -framework IOKit \
  -F /System/Library/PrivateFrameworks \
  -framework DisplayServices \
  "${SCRIPT_DIR}/src/DisplayBrightnessTool.swift" \
  -o "${BUILD_DIR}/DisplayBrightnessTool"

/bin/mkdir -p \
  "${RUNTIME_DIR}" \
  "${STATE_DIR}" \
  "${LOG_DIR}" \
  "${HOME}/Library/LaunchAgents"
/bin/chmod 700 "${INSTALL_ROOT}" "${RUNTIME_DIR}" "${STATE_DIR}" "${LOG_DIR}"

/usr/bin/install -m 0700 \
  "${SCRIPT_DIR}/uuremote_brightness_guard.py" \
  "${RUNTIME_DIR}/uuremote_brightness_guard.py"
/usr/bin/install -m 0700 \
  "${BUILD_DIR}/DisplayBrightnessTool" \
  "${RUNTIME_DIR}/DisplayBrightnessTool"

for control in status.sh restore.sh uninstall.sh Status.command Restore.command Uninstall.command; do
  /usr/bin/install -m 0700 "${SCRIPT_DIR}/${control}" "${INSTALL_ROOT}/${control}"
done

"${SCRIPT_DIR}/scripts/render_launchagent.sh" \
  "${SOURCE_PLIST}" \
  "${BUILD_DIR}/${LABEL}.plist" \
  "${RUNTIME_DIR}" \
  "${STATE_DIR}" \
  "${LOG_DIR}/guard.log" \
  "${LOG_DIR}/guard.error.log"

if [[ -f "${TARGET_PLIST}" ]]; then
  /bin/cp -p "${TARGET_PLIST}" "${BUILD_DIR}/previous.plist"
fi

/bin/launchctl bootout "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
if ! wait_until_agent_is_unloaded; then
  print -u2 -- "The previous LaunchAgent did not finish stopping. Its configuration was not replaced."
  exit 1
fi
/usr/bin/install -m 0644 "${BUILD_DIR}/${LABEL}.plist" "${TARGET_PLIST}"

if ! bootstrap_agent "${TARGET_PLIST}"; then
  print -u2 -- "The LaunchAgent could not be loaded. Rolling back the previous agent."
  if [[ -f "${BUILD_DIR}/previous.plist" ]]; then
    /usr/bin/install -m 0644 "${BUILD_DIR}/previous.plist" "${TARGET_PLIST}"
    bootstrap_agent "${TARGET_PLIST}" >/dev/null 2>&1 || true
  fi
  exit 1
fi

/bin/launchctl enable "${DOMAIN}/${LABEL}"
/bin/launchctl kickstart -k "${DOMAIN}/${LABEL}"

if ! /bin/launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  print -u2 -- "Installation finished, but the LaunchAgent is not running."
  exit 1
fi

print -- ""
print -- "UU Remote Brightness Guard is installed and running."
print -- "Connect: save each display and dim it to the minimum."
print -- "Disconnect: restore brightness, then sleep the displays."
print -- "Controls: ${INSTALL_ROOT}"
