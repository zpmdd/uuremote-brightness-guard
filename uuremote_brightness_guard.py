#!/usr/bin/env python3
"""Dim local displays while this Mac is controlled by UU Remote."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
MONITOR_CONTROL_EXECUTABLE = "/Applications/MonitorControl.app/Contents/MacOS/MonitorControl"
MONITOR_CONTROL_BUNDLE_ID = "app.monitorcontrol.MonitorControl"
UU_SERVER_EXECUTABLE = "/Applications/UURemote.app/Contents/Helpers/UURemoteServer"
BOOT_TIME_RE = re.compile(r"sec\s*=\s*(\d+)")
PROC_PIDLISTFDS = 1
PROC_PIDFDSOCKETINFO = 3
PROX_FDTYPE_SOCKET = 2
# offsetof(socket_fdinfo, psi.soi_protocol) in the Apple Silicon macOS ABI.
SOCKET_PROTOCOL_OFFSET = 180
SOCKET_FDINFO_BUFFER_SIZE = 1024
POST_WAKE_PHASES = {
    "awaiting-display-wake",
    "post-wake-verification-pending",
    "post-wake-repair-pending",
}


class ProcFDInfo(ctypes.Structure):
    _fields_ = [("fd", ctypes.c_int32), ("type", ctypes.c_uint32)]


LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")
LIBPROC.proc_pidinfo.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint64,
    ctypes.c_void_p,
    ctypes.c_int,
]
LIBPROC.proc_pidinfo.restype = ctypes.c_int
LIBPROC.proc_pidfdinfo.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
]
LIBPROC.proc_pidfdinfo.restype = ctypes.c_int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(event: str, **fields: object) -> None:
    payload = {"ts": utc_now(), "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def system_boot_epoch() -> float:
    override = os.environ.get("UURBG_BOOT_EPOCH")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        match = BOOT_TIME_RE.search(result.stdout)
        if match:
            return float(match.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return time.time()


class BrightnessGuard:
    def __init__(self) -> None:
        default_state_dir = Path.home() / "Library/Application Support/UURemoteBrightnessGuard"
        self.state_dir = Path(os.environ.get("UURBG_STATE_DIR", str(default_state_dir)))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "guard-state.json"
        self.snapshot_file = self.state_dir / "brightness-snapshot.json"
        self.lock_file = self.state_dir / "guard.lock"

        default_helper = self.state_dir / "DisplayBrightnessTool"
        self.helper = Path(os.environ.get("UURBG_HELPER_PATH", str(default_helper)))
        self.fallback = env_float("UURBG_FALLBACK", 0.85, 0.0, 1.0)
        self.ddc_fallback = env_float("UURBG_DDC_FALLBACK", 0.70, 0.0, 1.0)
        try:
            self.monitor_startup_action = int(os.environ.get("UURBG_MONITOR_STARTUP_ACTION", "1"))
        except ValueError:
            self.monitor_startup_action = 1
        self.monitor_startup_action = max(0, min(2, self.monitor_startup_action))
        self.dim_factor = env_float("UURBG_DIM_FACTOR", 0.0, 0.0, 1.0)
        self.disconnect_grace = env_float("UURBG_DISCONNECT_GRACE", 2.0, 0.0, 30.0)
        self.poll_interval = env_float("UURBG_POLL_INTERVAL", 0.25, 0.1, 5.0)
        self.sleep_after_disconnect = env_bool("UURBG_SLEEP_AFTER_DISCONNECT", True)
        self.display_sleep_delay = env_float("UURBG_DISPLAY_SLEEP_DELAY", 1.0, 0.0, 10.0)
        self.post_wake_delay = env_float("UURBG_POST_WAKE_DELAY", 4.0, 1.0, 30.0)
        self.post_wake_retry = env_float("UURBG_POST_WAKE_RETRY", 3.0, 1.0, 60.0)

        self.session_active = False
        self.server_pids: List[int] = []
        self.next_server_pid_refresh = 0.0
        self.restore_deadline: Optional[float] = None
        self.post_wake_deadline: Optional[float] = None
        self.display_sleep_pending = False
        self.shutdown_requested = False
        self._lock_handle = None
        self.holder_process: Optional[subprocess.Popen] = None
        self.event_monitor_process: Optional[subprocess.Popen] = None
        self.last_event_monitor_attempt = 0.0
        self.boot_epoch = system_boot_epoch()

    def acquire_lock(self) -> None:
        self._lock_handle = self.lock_file.open("a+")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another guard process is already running") from exc

    def load_state(self) -> Optional[Dict[str, object]]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if raw.get("schemaVersion") != SCHEMA_VERSION:
            return None
        return raw

    def save_state(self, state: Dict[str, object]) -> None:
        payload = {"schemaVersion": SCHEMA_VERSION, **state}
        fd, temporary = tempfile.mkstemp(prefix="guard-state.", suffix=".tmp", dir=str(self.state_dir))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_file)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def clear_state(self) -> None:
        for path in (self.state_file, self.snapshot_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def exact_process_pids(executable: str) -> List[int]:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", f"^{re.escape(executable)}$"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        pids: List[int] = []
        for token in result.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        return pids

    @staticmethod
    def process_udp_socket_count(pid: int) -> Optional[int]:
        needed = LIBPROC.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
        if needed <= 0:
            return None
        capacity = needed // ctypes.sizeof(ProcFDInfo) + 32
        entries = (ProcFDInfo * capacity)()
        used = LIBPROC.proc_pidinfo(
            pid,
            PROC_PIDLISTFDS,
            0,
            ctypes.byref(entries),
            ctypes.sizeof(entries),
        )
        if used <= 0:
            return None

        count = 0
        socket_info = ctypes.create_string_buffer(SOCKET_FDINFO_BUFFER_SIZE)
        for entry in entries[: used // ctypes.sizeof(ProcFDInfo)]:
            if entry.type != PROX_FDTYPE_SOCKET:
                continue
            detail_size = LIBPROC.proc_pidfdinfo(
                pid,
                entry.fd,
                PROC_PIDFDSOCKETINFO,
                socket_info,
                len(socket_info),
            )
            if detail_size < SOCKET_PROTOCOL_OFFSET + ctypes.sizeof(ctypes.c_int32):
                continue
            protocol = ctypes.c_int32.from_buffer(socket_info, SOCKET_PROTOCOL_OFFSET).value
            if protocol == socket.IPPROTO_UDP:
                count += 1
        return count

    def observe_session(self) -> Optional[bool]:
        now = time.monotonic()
        if not self.server_pids and now >= self.next_server_pid_refresh:
            self.server_pids = self.exact_process_pids(UU_SERVER_EXECUTABLE)
            self.next_server_pid_refresh = now + 5
        if not self.server_pids:
            return False

        observed = False
        for pid in self.server_pids:
            count = self.process_udp_socket_count(pid)
            if count is None:
                continue
            observed = True
            if count > 0:
                return True
        if observed:
            return False

        self.server_pids = []
        self.next_server_pid_refresh = 0
        return None

    def update_session(self) -> Optional[Tuple[bool, bool]]:
        observed = self.observe_session()
        if observed is None or observed == self.session_active:
            return None
        previous = self.session_active
        self.session_active = observed
        return previous, observed

    def state_is_stale(self, state: Dict[str, object]) -> bool:
        saved = state.get("bootEpoch")
        return not isinstance(saved, (int, float)) or abs(float(saved) - self.boot_epoch) > 1

    @staticmethod
    def stop_pids(pids: Iterable[int]) -> List[int]:
        stopped: List[int] = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGSTOP)
                stopped.append(pid)
            except ProcessLookupError:
                continue
            except PermissionError:
                log_event("monitorcontrol_pause_failed", reason="permission")
        return stopped

    @staticmethod
    def resume_pids(pids: Iterable[int]) -> None:
        for pid in set(pids):
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                continue
            except PermissionError:
                log_event("monitorcontrol_resume_failed", reason="permission")

    def restart_monitor_control(self, pids: Iterable[int]) -> bool:
        targets = sorted(set(pids))
        if not targets:
            return True
        startup_action = self.monitor_startup_action
        try:
            read_result = subprocess.run(
                ["/usr/bin/defaults", "read", MONITOR_CONTROL_BUNDLE_ID, "startupAction"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if read_result.returncode == 0:
                startup_action = int(read_result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

        def restore_startup_action() -> None:
            try:
                subprocess.run(
                    [
                        "/usr/bin/defaults",
                        "write",
                        MONITOR_CONTROL_BUNDLE_ID,
                        "startupAction",
                        "-int",
                        str(startup_action),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=3,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            subprocess.run(
                [
                    "/usr/bin/defaults",
                    "write",
                    MONITOR_CONTROL_BUNDLE_ID,
                    "startupAction",
                    "-int",
                    "2",
                ],
                check=False,
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.resume_pids(targets)
        for pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                log_event("monitorcontrol_restart_failed", reason="permission")
                restore_startup_action()
                return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(self.process_exists(pid) for pid in targets):
                break
            time.sleep(0.1)
        remaining = [pid for pid in targets if self.process_exists(pid)]
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
        launched = False
        try:
            subprocess.run(
                ["/usr/bin/open", "-a", "MonitorControl"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            log_event("monitorcontrol_restart_failed", reason="launch")
            restore_startup_action()
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.exact_process_pids(MONITOR_CONTROL_EXECUTABLE):
                launched = True
                break
            time.sleep(0.1)
        if launched:
            time.sleep(2)
        restore_startup_action()
        if launched:
            time.sleep(0.5)
            restore_startup_action()
        if launched:
            log_event("monitorcontrol_restarted", startupMode="read")
            return True
        log_event("monitorcontrol_restart_failed", reason="timeout")
        return False

    @staticmethod
    def process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def event_monitor_is_alive(self) -> bool:
        process = self.event_monitor_process
        return process is not None and process.poll() is None

    def event_monitor_detected(self) -> bool:
        if self.event_monitor_is_alive():
            return True
        try:
            result = subprocess.run(
                ["/bin/ps", "-axo", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        prefix = f"{self.helper} events --parent-pid "
        return any(line.strip().startswith(prefix) for line in result.stdout.splitlines())

    def start_event_monitor(self) -> bool:
        if self.event_monitor_is_alive():
            return True
        self.last_event_monitor_attempt = time.monotonic()
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            log_event("display_event_monitor_failed", reason="helper-missing")
            return False
        try:
            process = subprocess.Popen(
                [str(self.helper), "events", "--parent-pid", str(os.getpid())],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log_event("display_event_monitor_failed", reason=type(exc).__name__)
            return False

        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        if not ready:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            log_event("display_event_monitor_failed", reason="readiness-timeout")
            return False
        try:
            payload = json.loads(process.stdout.readline())
        except json.JSONDecodeError:
            payload = {}
        if (
            process.poll() is not None
            or not isinstance(payload, dict)
            or payload.get("action") != "events"
            or payload.get("event") != "ready"
        ):
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            log_event("display_event_monitor_failed", reason="invalid-readiness")
            return False
        self.event_monitor_process = process
        log_event("display_event_monitor_started")
        return True

    def stop_event_monitor(self) -> None:
        process = self.event_monitor_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self.event_monitor_process = None

    def handle_display_event(self, event: str) -> None:
        if event == "sleep":
            log_event("display_sleep_detected")
            return
        if event != "wake":
            return
        log_event("display_wake_detected")
        state = self.load_state()
        if (
            state is None
            or state.get("phase") not in POST_WAKE_PHASES
            or not self.snapshot_file.exists()
            or self.session_active
        ):
            return
        state["phase"] = "post-wake-verification-pending"
        state["displayWokeAt"] = utc_now()
        self.save_state(state)
        self.post_wake_deadline = time.monotonic() + self.post_wake_delay
        log_event("post_wake_verification_scheduled", delaySeconds=self.post_wake_delay)

    def poll_display_events(self) -> None:
        process = self.event_monitor_process
        if process is None:
            return
        assert process.stdout is not None
        while True:
            ready, _, _ = select.select([process.stdout], [], [], 0)
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("action") == "events":
                self.handle_display_event(str(payload.get("event", "")))
        if process.poll() is not None:
            log_event("display_event_monitor_stopped", exitCode=process.returncode)
            self.event_monitor_process = None

    def request_display_sleep(self) -> bool:
        if not self.sleep_after_disconnect:
            return False
        if not self.start_event_monitor():
            log_event("display_sleep_failed", reason="event-monitor-unavailable")
            return False
        if self.display_sleep_delay > 0:
            time.sleep(self.display_sleep_delay)
        try:
            result = subprocess.run(
                ["/usr/bin/pmset", "displaysleepnow"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_event("display_sleep_failed", reason=type(exc).__name__)
            return False
        if result.returncode == 0:
            log_event("display_sleep_requested")
            return True
        log_event("display_sleep_failed", exitCode=result.returncode)
        return False

    def helper_call(self, arguments: List[str]) -> Tuple[bool, Dict[str, object]]:
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            log_event("helper_missing")
            return False, {}
        try:
            result = subprocess.run(
                [str(self.helper), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_event("helper_failed", reason=type(exc).__name__)
            return False, {}
        try:
            payload = json.loads(result.stdout) if result.stdout else {}
        except json.JSONDecodeError:
            payload = {}
        warnings = payload.get("warnings", []) if isinstance(payload, dict) else []
        fallback_count = 0
        if isinstance(payload, dict):
            for group in ("native", "ddc"):
                values = payload.get(group, [])
                if isinstance(values, list):
                    fallback_count += sum(
                        1 for item in values if isinstance(item, dict) and item.get("fallbackUsed") is True
                    )
        log_event(
            "helper_result",
            action=arguments[0] if arguments else "unknown",
            exitCode=result.returncode,
            success=payload.get("success") if isinstance(payload, dict) else None,
            warnings=len(warnings) if isinstance(warnings, list) else 0,
            fallbacks=fallback_count,
        )
        return result.returncode == 0, payload if isinstance(payload, dict) else {}

    def snapshot_helper_arguments(self, action: str) -> List[str]:
        return [
            action,
            "--snapshot",
            str(self.snapshot_file),
            "--fallback",
            str(self.fallback),
            "--ddc-fallback",
            str(self.ddc_fallback),
        ]

    @staticmethod
    def helper_succeeded(ok: bool, payload: Dict[str, object]) -> bool:
        return ok and payload.get("success") is True

    def verify_post_wake_restore(self) -> bool:
        state = self.load_state()
        if (
            state is None
            or state.get("phase") not in POST_WAKE_PHASES
            or not self.snapshot_file.exists()
        ):
            self.post_wake_deadline = None
            return False
        if self.session_active:
            self.post_wake_deadline = None
            log_event("post_wake_verification_deferred", reason="active-session")
            return False

        verify_arguments = self.snapshot_helper_arguments("verify")
        verify_ok, verify_payload = self.helper_call(verify_arguments)
        if self.helper_succeeded(verify_ok, verify_payload):
            self.clear_state()
            self.post_wake_deadline = None
            log_event("post_wake_brightness_verified", repaired=False)
            return True

        restore_arguments = self.snapshot_helper_arguments("restore")
        restore_ok, restore_payload = self.helper_call(restore_arguments)
        time.sleep(0.4)
        reverify_ok, reverify_payload = self.helper_call(verify_arguments)
        if self.helper_succeeded(reverify_ok, reverify_payload):
            self.clear_state()
            self.post_wake_deadline = None
            log_event(
                "post_wake_brightness_verified",
                repaired=True,
                restoreSuccess=self.helper_succeeded(restore_ok, restore_payload),
            )
            return True

        attempts = int(state.get("postWakeAttempts", 0)) + 1
        state.update({
            "phase": "post-wake-repair-pending",
            "lastPostWakeAttemptAt": utc_now(),
            "postWakeAttempts": attempts,
            "pausedMonitorControlPids": [],
            "sleepAfterRestore": False,
        })
        self.save_state(state)
        self.post_wake_deadline = time.monotonic() + self.post_wake_retry
        log_event(
            "post_wake_brightness_pending",
            attempts=attempts,
            retryAfterSeconds=self.post_wake_retry,
        )
        return False

    def holder_is_alive(self, pid: object) -> bool:
        try:
            numeric_pid = int(pid)
            os.kill(numeric_pid, 0)
        except (TypeError, ValueError, ProcessLookupError, PermissionError):
            return False
        result = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(numeric_pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        command = result.stdout.strip()
        return str(self.helper) in command and " hold " in f" {command} "

    def start_holder(self, reuse_snapshot: bool) -> Tuple[bool, Dict[str, object], Optional[int]]:
        arguments = [
            str(self.helper),
            "hold",
            "--snapshot",
            str(self.snapshot_file),
            "--dim-factor",
            str(self.dim_factor),
            "--fallback",
            str(self.fallback),
            "--ddc-fallback",
            str(self.ddc_fallback),
            "--parent-pid",
            str(os.getpid()),
        ]
        if reuse_snapshot:
            arguments.append("--reuse-snapshot")
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log_event("holder_failed", reason=type(exc).__name__)
            return False, {}, None

        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 20)
        if not ready:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            log_event("holder_failed", reason="readiness-timeout")
            return False, {}, None

        line = process.stdout.readline()
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {}
        if process.poll() is not None or not isinstance(payload, dict) or payload.get("action") != "dim":
            process.terminate()
            log_event("holder_failed", reason="invalid-readiness")
            return False, payload if isinstance(payload, dict) else {}, None

        self.holder_process = process
        warnings = payload.get("warnings", [])
        log_event(
            "holder_ready",
            success=payload.get("success"),
            warnings=len(warnings) if isinstance(warnings, list) else 0,
        )
        return True, payload, process.pid

    def stop_holder(self, state: Dict[str, object]) -> None:
        holder_pid = state.get("holderPid")
        process = self.holder_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        elif self.holder_is_alive(holder_pid):
            numeric_pid = int(holder_pid)
            os.kill(numeric_pid, signal.SIGTERM)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and self.holder_is_alive(numeric_pid):
                time.sleep(0.1)
            if self.holder_is_alive(numeric_pid):
                os.kill(numeric_pid, signal.SIGKILL)
        self.holder_process = None

    def engage(self) -> bool:
        existing = self.load_state()
        if existing and self.snapshot_file.exists():
            paused = [int(value) for value in existing.get("pausedMonitorControlPids", [])]
            paused += self.stop_pids(self.exact_process_pids(MONITOR_CONTROL_EXECUTABLE))
            existing["pausedMonitorControlPids"] = sorted(set(paused))
            existing["bootEpoch"] = self.boot_epoch
            if self.holder_is_alive(existing.get("holderPid")):
                existing["phase"] = "dimmed"
                self.save_state(existing)
                log_event("dim_already_active")
                return True
            ok, payload, holder_pid = self.start_holder(reuse_snapshot=True)
            if ok and holder_pid is not None:
                existing["phase"] = "dimmed"
                existing["holderPid"] = holder_pid
                existing["helperSuccess"] = payload.get("success")
                self.save_state(existing)
                log_event("dim_recovered")
                return True
            self.restore()
            return False

        monitor_pids = self.exact_process_pids(MONITOR_CONTROL_EXECUTABLE)
        state: Dict[str, object] = {
            "phase": "engaging",
            "engagedAt": utc_now(),
            "bootEpoch": self.boot_epoch,
            "pausedMonitorControlPids": monitor_pids,
        }
        self.save_state(state)
        stopped = self.stop_pids(monitor_pids)
        state["pausedMonitorControlPids"] = stopped
        self.save_state(state)

        ok, payload, holder_pid = self.start_holder(reuse_snapshot=False)
        if not ok or not self.snapshot_file.exists():
            log_event("dim_failed_cleanup")
            self.restore(force_fallback=not self.snapshot_file.exists())
            return False

        state["phase"] = "dimmed"
        state["dimmedAt"] = utc_now()
        state["helperSuccess"] = payload.get("success")
        state["holderPid"] = holder_pid
        self.save_state(state)
        log_event("dim_engaged", monitorControlPaused=len(stopped))
        return True

    def restore(
        self,
        force_fallback: bool = False,
        restart_monitor_control: bool = False,
        sleep_after_success: bool = False,
    ) -> bool:
        state = self.load_state() or {}
        paused = [int(value) for value in state.get("pausedMonitorControlPids", [])]
        current_monitor_pids: List[int] = []
        if restart_monitor_control:
            current_monitor_pids = self.exact_process_pids(MONITOR_CONTROL_EXECUTABLE)
            paused += self.stop_pids(current_monitor_pids)
            paused = sorted(set(paused))
        self.stop_holder(state)
        if force_fallback or not self.snapshot_file.exists():
            helper_arguments = [
                "fallback",
                "--value",
                str(self.fallback),
                "--ddc-value",
                str(self.ddc_fallback),
            ]
        else:
            helper_arguments = self.snapshot_helper_arguments("restore")
        ok, payload = self.helper_call(helper_arguments)

        if restart_monitor_control and current_monitor_pids:
            restarted = self.restart_monitor_control(current_monitor_pids)
            if restarted:
                time.sleep(1)
                verify_ok, verify_payload = self.helper_call(helper_arguments)
                ok = verify_ok
                payload = verify_payload
            else:
                ok = False
                self.resume_pids(current_monitor_pids)
        else:
            self.resume_pids(paused)
        success = self.helper_succeeded(ok, payload)
        if success:
            log_event("brightness_restored", usedFallback=force_fallback)
            if sleep_after_success:
                state.update({
                    "phase": "awaiting-display-wake",
                    "restoredAt": utc_now(),
                    "bootEpoch": self.boot_epoch,
                    "pausedMonitorControlPids": [],
                    "sleepAfterRestore": False,
                    "postWakeAttempts": 0,
                })
                self.save_state(state)
                if self.request_display_sleep():
                    log_event("post_wake_verification_armed")
                else:
                    self.clear_state()
                    log_event("post_wake_verification_skipped", reason="display-sleep-failed")
            else:
                self.clear_state()
        else:
            state.update({
                "phase": "restore-pending",
                "lastRestoreAttemptAt": utc_now(),
                "pausedMonitorControlPids": [],
                "sleepAfterRestore": sleep_after_success,
            })
            self.save_state(state)
            log_event("brightness_restore_pending")
        return success

    def status(self) -> Dict[str, object]:
        observed = self.observe_session()
        if observed is not None:
            self.session_active = observed
        state = self.load_state()
        return {
            "activeSessions": int(self.session_active),
            "dimmed": bool(state and state.get("phase") in {"engaging", "dimmed", "restore-pending"}),
            "phase": state.get("phase") if state else "idle",
            "snapshotExists": self.snapshot_file.exists(),
            "helperExists": self.helper.is_file(),
            "fallback": self.fallback,
            "ddcFallback": self.ddc_fallback,
            "dimFactor": self.dim_factor,
            "disconnectGraceSeconds": self.disconnect_grace,
            "sleepAfterDisconnect": self.sleep_after_disconnect,
            "displaySleepDelaySeconds": self.display_sleep_delay,
            "postWakeDelaySeconds": self.post_wake_delay,
            "postWakeRetrySeconds": self.post_wake_retry,
            "eventMonitorRunning": self.event_monitor_detected(),
        }

    def request_shutdown(self, _signum: int, _frame: object) -> None:
        self.shutdown_requested = True

    def run(self) -> int:
        self.acquire_lock()
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)
        signal.signal(signal.SIGHUP, self.request_shutdown)

        event_monitor_started = self.start_event_monitor()
        observed = self.observe_session()
        if observed is not None:
            self.session_active = observed
        log_event(
            "guard_started",
            activeSessions=int(self.session_active),
            recoveredState=self.load_state() is not None,
        )

        state = self.load_state()
        if self.session_active:
            self.engage()
        elif state is not None:
            if (
                state.get("phase") in POST_WAKE_PHASES
                and self.snapshot_file.exists()
                and not self.state_is_stale(state)
                and event_monitor_started
            ):
                log_event("post_wake_state_recovered", phase=state.get("phase"))
                if state.get("phase") != "awaiting-display-wake":
                    self.post_wake_deadline = time.monotonic() + self.post_wake_retry
            else:
                self.restore(restart_monitor_control=self.state_is_stale(state))

        last_restore_retry = 0.0
        while not self.shutdown_requested:
            now = time.monotonic()
            self.poll_display_events()
            if not self.event_monitor_is_alive() and now - self.last_event_monitor_attempt >= 10:
                self.start_event_monitor()
            transition = self.update_session()
            if transition:
                _, is_active = transition
                if is_active:
                    self.restore_deadline = None
                    self.post_wake_deadline = None
                    self.display_sleep_pending = False
                    log_event("session_connected", activeSessions=1)
                    self.engage()
                else:
                    self.restore_deadline = now + self.disconnect_grace
                    self.display_sleep_pending = self.sleep_after_disconnect
                    log_event(
                        "session_disconnected",
                        restoreAfterSeconds=self.disconnect_grace,
                    )

            if self.restore_deadline is not None and now >= self.restore_deadline:
                self.restore_deadline = None
                if not self.session_active:
                    sleep_after_success = self.display_sleep_pending
                    self.display_sleep_pending = False
                    self.restore(sleep_after_success=sleep_after_success)

            if self.post_wake_deadline is not None and now >= self.post_wake_deadline:
                self.post_wake_deadline = None
                if not self.session_active:
                    self.verify_post_wake_restore()

            state = self.load_state()
            if (
                state
                and state.get("phase") == "restore-pending"
                and not self.session_active
                and now - last_restore_retry >= 10
            ):
                last_restore_retry = now
                self.restore(
                    restart_monitor_control=self.state_is_stale(state),
                    sleep_after_success=bool(state.get("sleepAfterRestore")),
                )

            time.sleep(self.poll_interval)

        if self.load_state() is not None:
            self.restore()
        self.stop_event_monitor()
        log_event("guard_stopped")
        return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="print current guard state")
    group.add_argument("--restore-now", action="store_true", help="restore brightness and exit")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    guard = BrightnessGuard()
    if args.status:
        print(json.dumps(guard.status(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.restore_now:
        guard.acquire_lock()
        state = guard.load_state() or {}
        restart_monitor_control = bool(state) and guard.state_is_stale(state)
        return 0 if guard.restore(restart_monitor_control=restart_monitor_control) else 1
    try:
        return guard.run()
    except RuntimeError as exc:
        log_event("guard_not_started", reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
