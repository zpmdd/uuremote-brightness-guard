import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "uuremote_brightness_guard.py"
SPEC = importlib.util.spec_from_file_location("uuremote_brightness_guard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SessionDetectionTests(unittest.TestCase):
    IDLE_ASSERTIONS = '''
Assertion status system-wide:
   UserIsActive                   1
   PreventUserIdleDisplaySleep    0
Listed by owning process:
   pid 123(UURemoteServer): [0x1] 00:00:01 UserIsActive named: "Wake Up Display"
   pid 124(UURemote): [0x2] 01:00:00 PreventUserIdleSystemSleep named: "UURemote Disable Idle System Sleep"
'''
    ACTIVE_ASSERTIONS = IDLE_ASSERTIONS + '''
   pid 123(UURemoteServer): [0x3] 00:00:02 PreventUserIdleDisplaySleep named: "idleDisplaySleepDisabled"
'''

    def make_guard(self, temporary):
        original = os.environ.get("UURBG_STATE_DIR")
        os.environ["UURBG_STATE_DIR"] = temporary
        return MODULE.BrightnessGuard(), original

    def restore_environment(self, original):
        if original is None:
            os.environ.pop("UURBG_STATE_DIR", None)
        else:
            os.environ["UURBG_STATE_DIR"] = original

    @staticmethod
    def payload(message):
        return {
            "processID": 123,
            "processImagePath": MODULE.UU_SERVER_EXECUTABLE,
            "senderImagePath": MODULE.SCREEN_CAPTURE_SENDER,
            "eventMessage": message,
        }

    def test_only_screen_capture_events_activate_a_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                start_a = " [INFO] -[SCStream startCaptureWithCompletionHandler:]:816 0xaaa streamID=<private>"
                start_b = " [INFO] -[SCStream startCaptureWithCompletionHandler:]:816 0xbbb streamID=<private>"
                stop_a = " [INFO] -[SCStream stopCaptureWithCompletionHandler:]:852 0xaaa streamID=<private>"
                dealloc_b = " [INFO] -[SCStream dealloc]:589 0xbbb streamID=<private>"

                self.assertEqual(guard.handle_session_log_payload(self.payload(start_a)), (False, True))
                self.assertIsNone(guard.handle_session_log_payload(self.payload(start_b)))
                self.assertIsNone(guard.handle_session_log_payload(self.payload(stop_a)))
                self.assertEqual(guard.handle_session_log_payload(self.payload(dealloc_b)), (True, False))
                self.assertIsNone(guard.handle_session_log_payload(self.payload(dealloc_b)))
            finally:
                self.restore_environment(original)

    def test_power_assertion_is_negative_only_and_strictly_matched(self):
        self.assertEqual(MODULE.BrightnessGuard.parse_session_assertion_pids(self.IDLE_ASSERTIONS), set())
        for name in ("idleDisplaySleepDisabled", "UURemote Disable Display Sleep"):
            with self.subTest(assertion_name=name):
                active = self.ACTIVE_ASSERTIONS.replace("idleDisplaySleepDisabled", name)
                self.assertEqual(MODULE.BrightnessGuard.parse_session_assertion_pids(active), {123})
                for unrelated in (
                    active.replace("UURemoteServer", "AnotherProcess"),
                    active.replace("PreventUserIdleDisplaySleep", "PreventUserIdleSystemSleep"),
                    active.replace(name, "Unrelated Assertion"),
                ):
                    self.assertEqual(MODULE.BrightnessGuard.parse_session_assertion_pids(unrelated), set())
        self.assertIsNone(MODULE.BrightnessGuard.parse_session_assertion_pids("unexpected output"))

        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                with patch.object(guard, "observe_session_assertion_pids", return_value={123}) as probe:
                    self.assertIsNone(guard.poll_session_assertion(10))
                probe.assert_not_called()
                self.assertFalse(guard.session_active)
            finally:
                self.restore_environment(original)

    def test_active_assertion_names_prevent_false_disconnect_and_sleep(self):
        for name in ("idleDisplaySleepDisabled", "UURemote Disable Display Sleep"):
            with self.subTest(assertion_name=name), tempfile.TemporaryDirectory() as temporary:
                with patch.dict(os.environ, {"UURBG_STATE_DIR": temporary}):
                    guard = MODULE.BrightnessGuard()
                guard.disconnect_grace = 2
                guard.sleep_after_disconnect = True
                start = self.payload(
                    " [INFO] -[SCStream startCaptureWithCompletionHandler:]:816 0xaaa streamID=<private>"
                )
                self.assertEqual(guard.handle_session_log_payload(start), (False, True))
                completed = MODULE.subprocess.CompletedProcess(
                    [], 0, self.ACTIVE_ASSERTIONS.replace("idleDisplaySleepDisabled", name), ""
                )
                with (
                    patch.object(MODULE.subprocess, "run", return_value=completed) as mocked,
                    patch.object(guard, "start_event_monitor", return_value=True),
                ):
                    for now in (10, 11, 12, 20):
                        self.assertIsNone(guard.poll_session_assertion(now))
                    self.assertTrue(guard.session_active)
                    self.assertFalse(guard.request_display_sleep())
                    for call in mocked.call_args_list:
                        self.assertEqual(call.args[0], ["/usr/bin/pmset", "-g", "assertions"])

                    completed.stdout = self.IDLE_ASSERTIONS
                    stop = self.payload(
                        " [INFO] -[SCStream stopCaptureWithCompletionHandler:]:852 0xaaa streamID=<private>"
                    )
                    self.assertEqual(guard.handle_session_log_payload(stop), (True, False))
                    self.assertEqual(guard.observe_session_assertion_pids(), set())
                    self.assertTrue(guard.request_display_sleep())
                    self.assertEqual(mocked.call_args.args[0], ["/usr/bin/pmset", "displaysleepnow"])

    def test_missing_assertion_eventually_fails_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.session_active = True
                guard.active_streams.add((123, "0xaaa"))
                guard.disconnect_grace = 2
                with patch.object(guard, "observe_session_assertion_pids", return_value=None):
                    self.assertIsNone(guard.poll_session_assertion(10))
                    self.assertIsNone(guard.poll_session_assertion(11))
                    self.assertEqual(guard.poll_session_assertion(12), (True, False))
                self.assertFalse(guard.session_active)
            finally:
                self.restore_environment(original)

    def test_stopped_session_monitor_fails_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.session_active = True
                guard.active_streams.add((123, "0xaaa"))
                process = MagicMock()
                process.stdout.readline.return_value = b""
                process.poll.return_value = 1
                process.returncode = 1
                guard.session_monitor_process = process
                with patch.object(MODULE.select, "select", return_value=([process.stdout], [], [])):
                    self.assertEqual(guard.poll_session_events(), [(True, False)])
                self.assertFalse(guard.session_active)
            finally:
                self.restore_environment(original)

    def test_queued_start_and_stop_are_collapsed_to_the_final_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                start = json.dumps(self.payload(
                    " [INFO] -[SCStream startCaptureWithCompletionHandler:]:816 0xaaa streamID=<private>"
                )).encode() + b"\n"
                stop = json.dumps(self.payload(
                    " [INFO] -[SCStream stopCaptureWithCompletionHandler:]:852 0xaaa streamID=<private>"
                )).encode() + b"\n"
                process = MagicMock()
                process.stdout.readline.side_effect = [start, stop]
                process.poll.return_value = None
                guard.session_monitor_process = process
                readiness = [([process.stdout], [], []), ([process.stdout], [], []), ([], [], [])]
                with patch.object(MODULE.select, "select", side_effect=readiness):
                    self.assertEqual(guard.poll_session_events(), [])
                self.assertFalse(guard.session_active)
            finally:
                self.restore_environment(original)

    def test_active_assertion_bridges_capture_stream_turnover(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.session_active = True
                guard.active_streams.add((123, "0xaaa"))
                stop = json.dumps(self.payload(
                    " [INFO] -[SCStream stopCaptureWithCompletionHandler:]:852 0xaaa streamID=<private>"
                )).encode() + b"\n"
                process = MagicMock()
                process.stdout.readline.return_value = stop
                process.poll.return_value = None
                guard.session_monitor_process = process
                readiness = [([process.stdout], [], []), ([], [], [])]
                with (
                    patch.object(MODULE.select, "select", side_effect=readiness),
                    patch.object(guard, "observe_session_assertion_pids", return_value={123}),
                ):
                    self.assertEqual(guard.poll_session_events(), [])
                self.assertTrue(guard.session_active)
                self.assertEqual(guard.active_streams, set())
                guard.disconnect_grace = 2
                with patch.object(guard, "observe_session_assertion_pids", return_value={123}):
                    self.assertIsNone(guard.poll_session_assertion(10))
                    self.assertIsNone(guard.poll_session_assertion(11))
                    self.assertEqual(guard.poll_session_assertion(12), (True, False))
            finally:
                self.restore_environment(original)


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
                guard.last_good_file.write_text("{}", encoding="utf-8")
                guard.clear_state()
                self.assertFalse(guard.state_file.exists())
                self.assertFalse(guard.snapshot_file.exists())
                self.assertTrue(guard.last_good_file.exists())
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


class SnapshotCaptureTests(unittest.TestCase):
    def test_new_capture_protects_wake_sleep_and_unknown_state_but_not_stable_manual_zero(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"UURBG_STATE_DIR": temporary}):
            guard = MODULE.BrightnessGuard()
            guard.post_wake_delay = 4
            guard.poll_display_events = MagicMock()
            guard.event_monitor_is_alive = MagicMock(return_value=True)
            process = MagicMock()
            process.poll.return_value = None
            process.stdout.readline.return_value = '{"action":"dim","success":true}\n'
            with (
                patch.object(MODULE.subprocess, "Popen", return_value=process) as popen,
                patch.object(MODULE.select, "select", return_value=([process.stdout], [], [])),
                patch.object(MODULE.time, "monotonic", return_value=100),
            ):
                for last_wake, reuse, protected in (
                    (None, False, True), (99.967, False, True),
                    (96, False, False), (None, True, False),
                ):
                    with self.subTest(last_wake=last_wake, reuse=reuse):
                        guard.last_display_wake = last_wake
                        self.assertTrue(guard.start_holder(reuse)[0])
                        arguments = popen.call_args.args[0]
                        self.assertEqual("--protect-wake-snapshot" in arguments, protected)
                        self.assertEqual("--reuse-snapshot" in arguments, reuse)
                        self.assertEqual(arguments[arguments.index("--last-good") + 1], str(guard.last_good_file))
                guard.event_monitor_is_alive.return_value = False
                guard.last_display_wake = 0
                guard.start_holder(False)
                self.assertIn("--protect-wake-snapshot", popen.call_args.args[0])
                guard.event_monitor_is_alive.return_value = True
                guard.poll_display_events.side_effect = lambda: guard.handle_display_event("wake")
                guard.start_holder(False)
                self.assertIn("--protect-wake-snapshot", popen.call_args.args[0])

    def test_idle_sleep_and_wake_are_tracked_without_a_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"UURBG_STATE_DIR": temporary}):
            guard = MODULE.BrightnessGuard()
            with patch.object(MODULE.time, "monotonic", return_value=100):
                guard.handle_display_event("wake")
            self.assertEqual(guard.last_display_wake, 100)
            guard.handle_display_event("sleep")
            self.assertIsNone(guard.last_display_wake)
            self.assertIsNone(guard.load_state())


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
                    patch.object(guard, "observe_session_assertion_pids", return_value=set()),
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

    def test_display_sleep_is_skipped_while_uu_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                with (
                    patch.object(guard, "start_event_monitor", return_value=True),
                    patch.object(guard, "observe_session_assertion_pids", return_value={123}),
                    patch.object(MODULE.subprocess, "run") as mocked,
                ):
                    self.assertFalse(guard.request_display_sleep())
                mocked.assert_not_called()
            finally:
                self.restore_environment(original)

    def test_display_sleep_is_skipped_when_session_state_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                with (
                    patch.object(guard, "start_event_monitor", return_value=True),
                    patch.object(guard, "observe_session_assertion_pids", return_value=None),
                    patch.object(MODULE.subprocess, "run") as mocked,
                ):
                    self.assertFalse(guard.request_display_sleep())
                mocked.assert_not_called()
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

    def test_successful_restore_schedules_display_sleep(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.snapshot_file.write_text("{}", encoding="utf-8")
                guard.stop_holder = lambda _state: None
                guard.helper_call = lambda _arguments: (True, {"success": True})
                with (
                    patch.object(guard, "start_event_monitor", return_value=True),
                    patch.object(MODULE.time, "monotonic", return_value=100),
                ):
                    self.assertTrue(guard.restore(sleep_after_success=True))
                self.assertTrue(guard.snapshot_file.exists())
                state = guard.load_state()
                self.assertIsNotNone(state)
                self.assertEqual(state["phase"], "display-sleep-pending")
                self.assertEqual(guard.display_sleep_deadline, 100 + guard.display_sleep_delay)
            finally:
                self.restore_environment(original)

    def test_failed_display_sleep_clears_an_already_restored_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.snapshot_file.write_text("{}", encoding="utf-8")
                guard.stop_holder = lambda _state: None
                guard.helper_call = lambda _arguments: (True, {"success": True})
                with patch.object(guard, "start_event_monitor", return_value=False):
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
        guard.exact_process_pids = MagicMock(return_value=[])
        guard.snapshot_file.write_text("{}", encoding="utf-8")
        guard.save_state({
            "phase": "post-wake-verification-pending",
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

    def test_sleep_cancels_verification_and_preserves_snapshot_until_next_wake(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.helper_call = MagicMock(return_value=(True, {"success": True}))
                guard.post_wake_delay = 4
                with patch.object(MODULE.time, "monotonic", return_value=100):
                    guard.handle_display_event("wake")
                guard.handle_display_event("sleep")
                self.assertIsNone(guard.post_wake_deadline)
                self.assertEqual(guard.load_state()["phase"], "awaiting-display-wake")
                self.assertFalse(guard.verify_post_wake_restore())
                guard.helper_call.assert_not_called()
                self.assertTrue(guard.snapshot_file.exists())
                with patch.object(MODULE.time, "monotonic", return_value=110):
                    guard.handle_display_event("wake")
                    self.assertEqual(guard.post_wake_deadline, 114)
                    self.assertFalse(guard.verify_post_wake_restore())
                with patch.object(MODULE.time, "monotonic", return_value=114):
                    self.assertTrue(guard.verify_post_wake_restore())
                guard.helper_call.assert_called_once()
            finally:
                self.restore_environment(original)

    def test_sleep_during_helper_stops_the_transaction_without_discarding_snapshot(self):
        for interrupt_on in (1, 2, 3):
            with self.subTest(helperCall=interrupt_on), tempfile.TemporaryDirectory() as temporary:
                guard, original = self.make_guard(temporary)
                try:
                    actions = []

                    def helper_call(arguments):
                        actions.append(arguments[0])
                        if len(actions) == interrupt_on:
                            guard.handle_display_event("sleep")
                        return True, {"success": len(actions) != 1 or interrupt_on == 1}

                    guard.helper_call = helper_call
                    with patch.object(MODULE.time, "sleep"):
                        self.assertFalse(guard.verify_post_wake_restore())
                    self.assertEqual(len(actions), interrupt_on)
                    self.assertIsNone(guard.post_wake_deadline)
                    self.assertEqual(guard.load_state()["phase"], "awaiting-display-wake")
                    self.assertTrue(guard.snapshot_file.exists())
                finally:
                    self.restore_environment(original)

    def test_new_wake_during_verification_restarts_the_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.post_wake_delay = 4

                def helper_call(arguments):
                    guard.handle_display_event("sleep")
                    guard.handle_display_event("wake")
                    return True, {"success": True}

                guard.helper_call = MagicMock(side_effect=helper_call)
                with patch.object(MODULE.time, "monotonic", return_value=100):
                    self.assertFalse(guard.verify_post_wake_restore())
                    self.assertEqual(guard.post_wake_deadline, 104)
                    self.assertFalse(guard.verify_post_wake_restore())
                guard.helper_call.assert_called_once()
                self.assertTrue(guard.snapshot_file.exists())
                self.assertEqual(guard.load_state()["phase"], "post-wake-verification-pending")
            finally:
                self.restore_environment(original)

    def test_repeated_wake_does_not_allow_a_second_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                actions = []

                def helper_call(arguments):
                    actions.append(arguments[0])
                    if arguments[0] == "restore":
                        guard.handle_display_event("sleep")
                    return True, {"success": arguments[0] == "restore"}

                guard.helper_call = helper_call
                self.assertFalse(guard.verify_post_wake_restore())
                self.assertTrue(guard.load_state()["postWakeRepairAttempted"])
                guard.handle_display_event("wake")
                self.assertFalse(guard.verify_post_wake_restore())
                self.assertEqual(actions, ["verify", "restore", "verify"])
                self.assertIsNone(guard.load_state())
                self.assertFalse(guard.snapshot_file.exists())
            finally:
                self.restore_environment(original)

    def test_new_remote_session_resets_repair_budget_when_reusing_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                state = guard.load_state()
                state["postWakeRepairAttempted"] = True
                guard.save_state(state)
                guard.holder_is_alive = MagicMock(return_value=False)
                guard.start_holder = MagicMock(return_value=(True, {"success": True}, 123))
                self.assertTrue(guard.engage())
                guard.start_holder.assert_called_once_with(reuse_snapshot=True)
                self.assertEqual(guard.load_state()["phase"], "dimmed")
                self.assertNotIn("postWakeRepairAttempted", guard.load_state())
                self.assertTrue(guard.snapshot_file.exists())
            finally:
                self.restore_environment(original)

    def test_monitorcontrol_is_resumed_if_verification_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                guard.exact_process_pids.return_value = [123]

                def check_saved_pids(pids):
                    self.assertEqual(guard.load_state()["pausedMonitorControlPids"], pids)

                guard.stop_pids = MagicMock(side_effect=check_saved_pids)
                guard.resume_pids = MagicMock(side_effect=check_saved_pids)
                guard.helper_call = MagicMock(side_effect=RuntimeError("test error"))
                with self.assertRaisesRegex(RuntimeError, "test error"):
                    guard.verify_post_wake_restore()
                guard.stop_pids.assert_called_once_with([123])
                guard.resume_pids.assert_called_once_with([123])
                self.assertEqual(guard.load_state()["pausedMonitorControlPids"], [])
            finally:
                self.restore_environment(original)

    def test_restart_resumes_recorded_monitorcontrol_before_waiting_for_wake(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                state = guard.load_state()
                state["pausedMonitorControlPids"] = [123, 456]
                guard.save_state(state)
                guard.exact_process_pids.return_value = [123]
                resumed = []
                guard.resume_pids = lambda pids: resumed.extend(pids)
                for method in ("acquire_lock", "start_session_monitor", "start_event_monitor",
                               "restore", "stop_session_monitor", "stop_event_monitor"):
                    setattr(guard, method, MagicMock(return_value=True))
                guard.state_is_stale = MagicMock(return_value=False)
                guard.shutdown_requested = True
                with patch.object(MODULE.signal, "signal"):
                    guard.run()
                self.assertEqual(resumed, [123])
                self.assertEqual(guard.load_state()["pausedMonitorControlPids"], [])
            finally:
                self.restore_environment(original)

    def test_queued_display_events_are_not_hidden_by_pipe_buffering(self):
        for events in (("ready", "sleep"), ("ready", "wake", "sleep")):
            with self.subTest(events=events), tempfile.TemporaryDirectory() as temporary:
                guard, original = self.make_guard(temporary)
                read_fd, write_fd = os.pipe()
                process = MagicMock()
                process.poll.return_value = None
                try:
                    for event in events:
                        os.write(write_fd, (json.dumps({"action": "events", "event": event}) + "\n").encode())

                    def make_process(*args, **kwargs):
                        mode = "r" if kwargs.get("text") else "rb"
                        process.stdout = os.fdopen(read_fd, mode, buffering=kwargs["bufsize"])
                        return process

                    guard.helper = MODULE_PATH
                    guard.helper_call = MagicMock(return_value=(True, {"success": True}))
                    with patch.object(MODULE.os, "access", return_value=True), \
                            patch.object(MODULE.subprocess, "Popen", side_effect=make_process):
                        self.assertTrue(guard.start_event_monitor())
                        self.assertFalse(guard.verify_post_wake_restore())
                    guard.helper_call.assert_not_called()
                    self.assertEqual(guard.load_state()["phase"], "awaiting-display-wake")
                    self.assertIsNone(guard.post_wake_deadline)
                finally:
                    process.stdout.close()
                    os.close(write_fd)
                    self.restore_environment(original)

    def test_helper_diagnostics_keep_only_brightness_fields(self):
        diagnostics = MODULE.BrightnessGuard.helper_diagnostics({
            "warnings": ["ddc-0 verification mismatch", {"unexpected": "value"}],
            "ddc": [{"target": "ddc-0", "requested": 0.7, "observed": 0.1,
                     "displayID": 123, "serialNumber": "private"}],
            "gamma": [{"target": "gamma-0", "red": [0, 1]}],
        })
        self.assertEqual(diagnostics["ddc"], [{"target": "ddc-0", "requested": 0.7, "observed": 0.1}])
        self.assertEqual(diagnostics["gamma"], [{"target": "gamma-0"}])
        self.assertEqual(diagnostics["warningDetails"], ["ddc-0 verification mismatch"])

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

    def test_persistent_mismatch_stops_after_one_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard, original = self.make_guard(temporary)
            try:
                state = guard.load_state()
                self.assertIsNotNone(state)
                state["phase"] = "post-wake-repair-pending"
                guard.save_state(state)
                actions = []

                def helper_call(arguments):
                    actions.append(arguments[0])
                    return False, {"success": False, "warnings": ["ddc-0 verification mismatch"]}

                guard.helper_call = helper_call
                with patch.object(MODULE.time, "sleep"):
                    self.assertFalse(guard.verify_post_wake_restore())
                self.assertEqual(actions, ["verify", "restore", "verify"])
                self.assertIsNone(guard.load_state())
                self.assertFalse(guard.snapshot_file.exists())
                self.assertIsNone(guard.post_wake_deadline)
            finally:
                self.restore_environment(original)


if __name__ == "__main__":
    unittest.main()
