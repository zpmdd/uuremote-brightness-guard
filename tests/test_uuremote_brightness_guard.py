import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "uuremote_brightness_guard.py"
SPEC = importlib.util.spec_from_file_location("uuremote_brightness_guard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SessionTrackerTests(unittest.TestCase):
    def test_only_real_connected_and_disconnected_lines_change_state(self):
        tracker = MODULE.SessionTracker()
        connected = (
            "onPeerConnectionState(UUXPC.XPCPeerConnectionState(handle: 5, "
            "state: UUXPC.XPCPeerConnectionState.State.peerConnected))"
        )
        disconnected = (
            "onPeerConnectionState(UUXPC.XPCPeerConnectionState(handle: 5, "
            "state: UUXPC.XPCPeerConnectionState.State.disconnected))"
        )

        self.assertEqual(tracker.feed(connected), (False, True))
        self.assertTrue(tracker.active)
        self.assertIsNone(tracker.feed("PrivacyScreenManager- session connected"))
        self.assertEqual(tracker.feed(disconnected), (True, False))
        self.assertFalse(tracker.active)

    def test_multiple_sessions_restore_only_after_last_disconnect(self):
        tracker = MODULE.SessionTracker()
        template = (
            "onPeerConnectionState(UUXPC.XPCPeerConnectionState(handle: {handle}, "
            "state: UUXPC.XPCPeerConnectionState.State.{state}))"
        )
        self.assertEqual(tracker.feed(template.format(handle=1, state="peerConnected")), (False, True))
        self.assertIsNone(tracker.feed(template.format(handle=2, state="peerConnected")))
        self.assertIsNone(tracker.feed(template.format(handle=1, state="disconnected")))
        self.assertEqual(tracker.feed(template.format(handle=2, state="disconnected")), (True, False))


class SessionReconstructionTests(unittest.TestCase):
    def test_old_server_session_is_ignored_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log_dir = root / "logs"
            log_dir.mkdir()
            state_dir = root / "state"
            current_log = log_dir / "UURemoteServer.log"
            template = (
                "[{timestamp} 909/1][info] "
                "onPeerConnectionState(UUXPC.XPCPeerConnectionState(handle: {handle}, "
                "state: UUXPC.XPCPeerConnectionState.State.{state}))\n"
            )
            current_log.write_text(
                template.format(timestamp="08-06 21:43:03.000", handle=1, state="peerConnected")
                + template.format(timestamp="08-06 21:49:42.000", handle=2, state="peerConnected")
                + template.format(timestamp="08-06 21:53:41.000", handle=2, state="disconnected"),
                encoding="utf-8",
            )
            original = {name: os.environ.get(name) for name in ("UURBG_STATE_DIR", "UURBG_LOG_DIR")}
            os.environ["UURBG_STATE_DIR"] = str(state_dir)
            os.environ["UURBG_LOG_DIR"] = str(log_dir)
            try:
                guard = MODULE.BrightnessGuard()
                guard.boot_epoch = MODULE.log_line_epoch("[08-06 21:44:51.000 1/1]", None)
                server_started = MODULE.log_line_epoch("[08-06 21:45:57.000 1/1]", None)
                guard.process_start_epoch = lambda _executable: server_started
                guard.reconstruct_sessions()
                self.assertFalse(guard.tracker.active)
            finally:
                for name, value in original.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_log_timestamp_parser_rejects_lines_without_timestamp(self):
        self.assertIsNone(MODULE.log_line_epoch("onPeerConnectionState(...)", None))


class StateFileTests(unittest.TestCase):
    def test_state_round_trip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = MODULE.os.environ.get("UURBG_STATE_DIR")
            MODULE.os.environ["UURBG_STATE_DIR"] = temporary
            try:
                guard = MODULE.BrightnessGuard()
                guard.save_state({"phase": "dimmed", "pausedMonitorControlPids": [123]})
                state = guard.load_state()
                self.assertEqual(state["phase"], "dimmed")
                self.assertEqual(state["pausedMonitorControlPids"], [123])
                guard.snapshot_file.write_text(json.dumps({"test": True}), encoding="utf-8")
                guard.clear_state()
                self.assertFalse(guard.state_file.exists())
                self.assertFalse(guard.snapshot_file.exists())
            finally:
                if original is None:
                    MODULE.os.environ.pop("UURBG_STATE_DIR", None)
                else:
                    MODULE.os.environ["UURBG_STATE_DIR"] = original

    def test_state_from_another_boot_is_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = MODULE.os.environ.get("UURBG_STATE_DIR")
            MODULE.os.environ["UURBG_STATE_DIR"] = temporary
            try:
                guard = MODULE.BrightnessGuard()
                guard.boot_epoch = 2000
                self.assertFalse(guard.state_is_stale({"bootEpoch": 2000}))
                self.assertTrue(guard.state_is_stale({"bootEpoch": 1000}))
                self.assertTrue(guard.state_is_stale({}))
            finally:
                if original is None:
                    MODULE.os.environ.pop("UURBG_STATE_DIR", None)
                else:
                    MODULE.os.environ["UURBG_STATE_DIR"] = original


class DisplaySleepTests(unittest.TestCase):
    def make_guard(self, temporary):
        original = MODULE.os.environ.get("UURBG_STATE_DIR")
        MODULE.os.environ["UURBG_STATE_DIR"] = temporary
        guard = MODULE.BrightnessGuard()
        guard.display_sleep_delay = 0
        return guard, original

    def restore_environment(self, original):
        if original is None:
            MODULE.os.environ.pop("UURBG_STATE_DIR", None)
        else:
            MODULE.os.environ["UURBG_STATE_DIR"] = original

    def test_display_sleep_uses_pmset_without_system_sleep(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                completed = MODULE.subprocess.CompletedProcess([], 0, "", "")
                with (
                    patch.object(guard, "start_event_monitor", return_value=True),
                    patch.object(MODULE.subprocess, "run", return_value=completed) as mocked,
                ):
                    self.assertTrue(guard.request_display_sleep())
                mocked.assert_called_once_with(
                    ["/usr/bin/pmset", "displaysleepnow"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                self.restore_environment(original)

    def test_failed_restore_keeps_pending_sleep_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.snapshot_file.write_text("{}", encoding="utf-8")
                guard.stop_holder = lambda _state: None
                guard.helper_call = lambda _arguments: (False, {})
                self.assertFalse(guard.restore(sleep_after_success=True))
                state = guard.load_state()
                self.assertIsNotNone(state)
                self.assertTrue(state["sleepAfterRestore"])
            finally:
                self.restore_environment(original)

    def test_successful_restore_requests_display_sleep(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.snapshot_file.write_text("{}", encoding="utf-8")
                guard.stop_holder = lambda _state: None
                guard.helper_call = lambda _arguments: (True, {"success": True})
                with patch.object(guard, "request_display_sleep", return_value=True) as requested:
                    self.assertTrue(guard.restore(sleep_after_success=True))
                requested.assert_called_once_with()
                self.assertTrue(guard.snapshot_file.exists())
                state = guard.load_state()
                self.assertIsNotNone(state)
                self.assertEqual(state["phase"], "awaiting-display-wake")
            finally:
                self.restore_environment(original)

    def test_failed_display_sleep_clears_an_already_restored_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.snapshot_file.write_text("{}", encoding="utf-8")
                guard.stop_holder = lambda _state: None
                guard.helper_call = lambda _arguments: (True, {"success": True})
                with patch.object(guard, "request_display_sleep", return_value=False):
                    self.assertTrue(guard.restore(sleep_after_success=True))
                self.assertFalse(guard.snapshot_file.exists())
                self.assertIsNone(guard.load_state())
            finally:
                self.restore_environment(original)


class PostWakeVerificationTests(unittest.TestCase):
    def make_guard(self, temporary):
        original = MODULE.os.environ.get("UURBG_STATE_DIR")
        MODULE.os.environ["UURBG_STATE_DIR"] = temporary
        guard = MODULE.BrightnessGuard()
        guard.post_wake_delay = 0
        guard.post_wake_retry = 0
        guard.snapshot_file.write_text("{}", encoding="utf-8")
        guard.save_state({
            "phase": "awaiting-display-wake",
            "bootEpoch": guard.boot_epoch,
            "pausedMonitorControlPids": [],
        })
        return guard, original

    def restore_environment(self, original):
        if original is None:
            MODULE.os.environ.pop("UURBG_STATE_DIR", None)
        else:
            MODULE.os.environ["UURBG_STATE_DIR"] = original

    def test_wake_event_schedules_verification_and_preserves_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.handle_display_event("wake")
                state = guard.load_state()
                self.assertIsNotNone(state)
                self.assertEqual(state["phase"], "post-wake-verification-pending")
                self.assertIsNotNone(guard.post_wake_deadline)
                self.assertTrue(guard.snapshot_file.exists())
            finally:
                self.restore_environment(original)

    def test_matching_brightness_clears_snapshot_without_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                actions = []

                def helper_call(arguments):
                    actions.append(arguments[0])
                    return True, {"success": True}

                guard.helper_call = helper_call
                self.assertTrue(guard.verify_post_wake_restore())
                self.assertEqual(actions, ["verify"])
                self.assertFalse(guard.snapshot_file.exists())
                self.assertIsNone(guard.load_state())
            finally:
                self.restore_environment(original)

    def test_mismatch_is_repaired_and_reverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                results = iter([
                    (False, {"success": False}),
                    (True, {"success": True}),
                    (True, {"success": True}),
                ])
                actions = []

                def helper_call(arguments):
                    actions.append(arguments[0])
                    return next(results)

                guard.helper_call = helper_call
                with patch.object(MODULE.time, "sleep"):
                    self.assertTrue(guard.verify_post_wake_restore())
                self.assertEqual(actions, ["verify", "restore", "verify"])
                self.assertFalse(guard.snapshot_file.exists())
                self.assertIsNone(guard.load_state())
            finally:
                self.restore_environment(original)

    def test_persistent_mismatch_keeps_snapshot_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.helper_call = lambda _arguments: (False, {"success": False})
                with patch.object(MODULE.time, "sleep"):
                    self.assertFalse(guard.verify_post_wake_restore())
                state = guard.load_state()
                self.assertIsNotNone(state)
                self.assertEqual(state["phase"], "post-wake-repair-pending")
                self.assertEqual(state["postWakeAttempts"], 1)
                self.assertTrue(guard.snapshot_file.exists())
                self.assertIsNotNone(guard.post_wake_deadline)
            finally:
                self.restore_environment(original)


if __name__ == "__main__":
    unittest.main()
