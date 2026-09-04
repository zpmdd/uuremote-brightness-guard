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


class SessionDetectionTests(unittest.TestCase):
    def test_udp_socket_presence_drives_connect_and_disconnect_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = os.environ.get("UURBG_STATE_DIR")
            os.environ["UURBG_STATE_DIR"] = temporary
            try:
                guard = MODULE.BrightnessGuard()
                guard.server_pids = [123]
                guard.next_server_pid_refresh = float("inf")
                with patch.object(guard, "process_udp_socket_count", side_effect=[0, 8, 8, 0]):
                    self.assertIsNone(guard.update_session())
                    self.assertEqual(guard.update_session(), (False, True))
                    self.assertIsNone(guard.update_session())
                    self.assertEqual(guard.update_session(), (True, False))
            finally:
                if original is None:
                    os.environ.pop("UURBG_STATE_DIR", None)
                else:
                    os.environ["UURBG_STATE_DIR"] = original

    def test_probe_failure_keeps_last_known_session_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = os.environ.get("UURBG_STATE_DIR")
            os.environ["UURBG_STATE_DIR"] = temporary
            try:
                guard = MODULE.BrightnessGuard()
                guard.session_active = True
                guard.server_pids = [123]
                guard.next_server_pid_refresh = float("inf")
                with patch.object(guard, "process_udp_socket_count", return_value=None):
                    self.assertIsNone(guard.update_session())
                self.assertTrue(guard.session_active)
            finally:
                if original is None:
                    os.environ.pop("UURBG_STATE_DIR", None)
                else:
                    os.environ["UURBG_STATE_DIR"] = original


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
