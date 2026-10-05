"""Regression tests with simulated USB/driver responses, never physical motion."""

import importlib.util
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import astra_plotter as plotter


OPTIONS = {"port": "/dev/test-axidraw", "model": 1}
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" '
       'viewBox="0 0 297 210"><path d="M 20 20 L 30 30" fill="none" stroke="black"/></svg>')
CONTROLLER = {"motor_steps": [0, 0], "motor_microsteps": [16, 16], "pen_reported_up": True,
              "servo_power_on": True, "queue_idle": True, "motors_enabled": True}


@contextmanager
def fake_connection(_):
    yield object(), {"firmware": "regression fixture", "motor_power_detected": True, "idle": True}


class Driver:
    def __init__(self, code=0, stop=None, wait=False, warnings=None, distance=0.1):
        self.options = SimpleNamespace()
        self.params = SimpleNamespace(native_res_factor=1016.0)
        self.calls = []
        self.errors = SimpleNamespace(code=code)
        self.plot_status = SimpleNamespace(stopped=code)
        self.warnings = SimpleNamespace(return_text_list=lambda: warnings or [])
        self.time_estimate = 2.0
        self.distance_pendown = distance
        self.stop = stop
        self.wait = wait
        self.entered = threading.Event()

    def plot_run(self):
        self.calls.append(vars(self.options).copy())
        self.entered.set()
        if self.wait:
            if not self.stop.wait(3):
                raise RuntimeError("Test worker timed out")
            self.errors.code = self.plot_status.stopped = 103


class SettingsTests(unittest.TestCase):
    def test_conservative_explicit_settings(self):
        value = plotter.settings_for(OPTIONS)
        self.assertEqual(value["speed_pendown"], 25)
        self.assertEqual(value["speed_penup"], 30)
        self.assertEqual(value["accel"], 25)
        self.assertEqual(value["pen_pos_up"], 60)
        self.assertEqual(value["pen_pos_down"], 30)

    def test_invalid_settings_fail_instead_of_clamping(self):
        for change in ({"model": 4}, {"model": True}, {"speed_pendown": 51},
                       {"speed_pendown": 0}, {"speed_pendown": "25"}, {"port": None},
                       {"pen_pos_up": 30}, {"pen_pos_down": 101}, {"speed_penup": 76}, {"accel": 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                plotter.settings_for({**OPTIONS, **change})


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.serial = SimpleNamespace(write=Mock(side_effect=lambda data: len(data)),
                                      readline=Mock(return_value=b""), close=Mock())
        self.connection = plotter.LoggedConnection(self.serial, timeout=0.01)

    def test_partial_and_empty_reads_are_buffered_and_logged(self):
        self.serial.readline.side_effect = [b"", b"QM,0,", b"0,0,0\n"]
        self.connection.write(b"QM\r")
        self.assertEqual(self.connection.readline(), b"QM,0,0,0,0\n")
        self.assertEqual(self.connection.last_command, "QM")
        self.assertEqual(self.connection.last_response, "QM,0,0,0,0")
        self.assertEqual([e["direction"] for e in self.connection.events], ["tx", "rx"])

    def test_missing_reply_fails_quickly_and_is_not_a_swallowed_runtime_error(self):
        self.connection.write(b"QM\r")
        start = time.monotonic()
        with self.assertRaisesRegex(plotter.PlotterCommunicationError, "after QM") as caught:
            self.connection.readline()
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertNotIsInstance(caught.exception, RuntimeError)
        self.assertEqual(self.connection.events[-1]["direction"], "rx_timeout")
        self.serial.write.assert_called_once()

    def test_controller_error_and_usb_disconnect_fail_closed(self):
        self.connection.write(b"SP,1\r")
        self.serial.readline.return_value = b"Err: bad command\r\n"
        with self.assertRaisesRegex(plotter.PlotterCommunicationError, "Controller rejected SP,1"):
            self.connection.readline()
        self.serial.readline.side_effect = OSError("USB disconnected")
        with self.assertRaisesRegex(plotter.PlotterCommunicationError, "USB read failed"):
            self.connection.readline()
        self.serial.write.side_effect = OSError("USB disconnected")
        with self.assertRaisesRegex(plotter.PlotterCommunicationError, "USB write failed"):
            self.connection.write(b"QM\r")

    def test_controller_queries_and_queue_wait_are_read_only(self):
        query = Mock(side_effect=["100,200", "1", "0", "QM,0,0,0,0", "16,16",
                                  "QM,1,0,0,0", "QM,0,0,0,0"])
        with patch.dict("sys.modules", {"plotink": SimpleNamespace(ebb_serial=SimpleNamespace(query=query))}):
            state = plotter.controller_state(self.connection)
            plotter.wait_idle(self.connection)
        self.assertEqual(state["motor_steps"], [100, 200])
        self.assertEqual(state["motor_microsteps"], [16, 16])
        self.assertTrue(state["pen_reported_up"])
        self.assertFalse(state["servo_power_on"])
        self.assertEqual([c.args[1] for c in query.call_args_list],
                         ["QS\r", "QP\r", "QR\r", "QM\r", "QE\r", "QM\r", "QM\r"])

    def test_malformed_state_and_busy_queue_fail(self):
        query = Mock(side_effect=["bad", "1", "0", "QM,0,0,0,0", "16,16"])
        with patch.dict("sys.modules", {"plotink": SimpleNamespace(ebb_serial=SimpleNamespace(query=query))}):
            with self.assertRaises(plotter.PlotterCommunicationError):
                plotter.controller_state(self.connection)
            with self.assertRaises(plotter.PlotterCommunicationError):
                plotter.wait_idle(self.connection, timeout=0)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = SimpleNamespace()
        self.serial = SimpleNamespace(
            testPort=Mock(return_value=self.connection), closePort=Mock(),
            min_version=Mock(return_value=True), queryVersion=Mock(return_value="EBB 2.6.5"),
            query=Mock(side_effect=["0,300", "00"]))
        self.modules = patch.dict("sys.modules", {"plotink": SimpleNamespace(ebb_serial=self.serial)})
        self.modules.start()
        self.devices = patch.object(plotter, "discover", return_value={"devices": [{"port": OPTIONS["port"]}]})
        self.devices.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.devices.stop)

    def test_read_only_checks_and_connection_cleanup(self):
        with plotter.checked_connection(OPTIONS["port"]) as (connection, result):
            self.assertTrue(result["motor_power_detected"])
            self.assertEqual(result["power_adc_counts"], 300)
            self.assertIs(connection.connection, self.connection)
        self.assertEqual([c.args[1] for c in self.serial.query.call_args_list], ["QC\r", "QG\r"])
        self.serial.closePort.assert_called_once_with(connection)

    def test_unknown_device_is_never_opened(self):
        with self.assertRaises(RuntimeError), plotter.checked_connection("/dev/other"):
            pass
        self.serial.testPort.assert_not_called()

    def test_missing_usb_connection_fails(self):
        self.serial.testPort.return_value = None
        with self.assertRaises(RuntimeError), plotter.checked_connection(OPTIONS["port"]):
            pass

    def test_power_missing_malformed_or_busy_fails_closed(self):
        for responses in (["0,100"], [""], ["garbage"], ["0,300", "01"], ["0,300", ""]):
            self.serial.query.side_effect = responses
            self.serial.closePort.reset_mock()
            with self.subTest(responses=responses), self.assertRaises(RuntimeError):
                with plotter.checked_connection(OPTIONS["port"]):
                    pass
            self.serial.closePort.assert_called_once()

    def test_unsupported_firmware_fails_without_motion(self):
        self.serial.min_version.return_value = False
        with self.assertRaises(RuntimeError), plotter.checked_connection(OPTIONS["port"]):
            pass
        self.serial.query.assert_not_called()
        self.serial.closePort.assert_called_once()


class PlotterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plotter = plotter.Plotter(Path(self.temp.name), enabled=True)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.plotter.close)
        self.driver = Driver()
        for target, replacement in (
            ("preview_svg", Mock(return_value={"estimated_seconds": 2.0, "warnings": []})),
            ("checked_connection", fake_connection),
            ("make_driver", Mock(side_effect=self.make_driver)),
            ("controller_state", Mock(return_value=CONTROLLER)),
            ("wait_idle", Mock()),
        ):
            scope = patch.object(plotter, target, replacement)
            scope.start()
            self.addCleanup(scope.stop)

    def make_driver(self, *args, stop=None, **kwargs):
        self.driver.stop = stop
        self.driver.options = SimpleNamespace(**args[1], mode="plot")
        return self.driver

    def prepare(self, home=True):
        if home:
            self.plotter.remember_home(OPTIONS, {"firmware": "regression fixture"}, CONTROLLER)
        return self.plotter.prepare("aaaaaaaaaaaa", SVG, OPTIONS)

    def start(self, prepared):
        return self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, homed=True)

    def test_disabled_plotter_cannot_prepare(self):
        self.plotter.enabled = False
        with self.assertRaises(RuntimeError):
            self.prepare()
        plotter.preview_svg.assert_not_called()

    def test_preflight_alone_never_starts_worker_or_moves(self):
        prepared = self.prepare()
        self.assertTrue(prepared["token"])
        self.assertNotIn("svg", prepared)
        self.assertIsNone(self.plotter.worker)
        plotter.make_driver.assert_not_called()

    def test_unknown_home_blocks_plot_without_consuming_preflight(self):
        prepared = self.prepare(home=False)
        self.assertFalse(prepared["home_ready"])
        for action in ("plot", "home", "motion_test"):
            with self.assertRaisesRegex(ValueError, "Set Home first"):
                self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, action, True, homed=True)
        self.assertIsNone(self.plotter.worker)
        self.assertEqual(self.plotter.prepared["token"], prepared["token"])

    def test_set_home_releases_enabled_motors_and_requires_corner_confirmation(self):
        prepared = self.prepare(home=False)
        with self.assertRaisesRegex(ValueError, "Home corner"):
            self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, "set_home")
        released = {**CONTROLLER, "motor_steps": [100, 200], "motor_microsteps": [0, 0], "motors_enabled": False}
        at_home = {**CONTROLLER, "motor_steps": [0, 0]}
        with patch.object(plotter, "controller_state",
                          side_effect=[CONTROLLER, CONTROLLER, released, released, at_home]):
            prepared = self.prepare(home=False)
            self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, "set_home", homed=True)
            self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual([(c["mode"], c.get("manual_cmd")) for c in self.driver.calls],
                         [("align", None), ("manual", "raise_pen"), ("manual", "enable_xy")])
        self.assertFalse(result["home"]["after_release"]["motors_enabled"])
        self.assertTrue(result["home"]["controller_verified"])
        self.assertFalse(result["home"]["physical_position_verified"])
        self.assertTrue(self.plotter.snapshot()["home_ready"])

    def test_plot_raises_pen_returns_home_verifies_then_draws(self):
        away = {**CONTROLLER, "motor_steps": [2400, 800]}
        self.plotter.remember_home(OPTIONS, {"firmware": "regression fixture"}, away)
        with patch.object(plotter, "controller_state", side_effect=[away, away, away, CONTROLLER, CONTROLLER]):
            self.start(self.prepare(home=False))
            self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual([(c["mode"], c.get("manual_cmd")) for c in self.driver.calls],
                         [("manual", "raise_pen"), ("manual", "walk_home"), ("plot", None)])
        self.assertEqual(result["home"]["from_mm"], [20, 10])
        self.assertEqual(self.driver.calls[1]["speed_penup"], 15)
        self.assertEqual(self.driver.calls[1]["accel"], 20)
        self.assertEqual(self.driver.calls[2]["speed_penup"], 30)
        self.assertEqual(self.driver.calls[2]["accel"], 25)
        self.assertTrue(result["home"]["controller_verified"])
        self.assertFalse(result["physical_output_verified"])

    def test_standalone_home_returns_without_plotting_or_needing_a_drawing(self):
        away = {**CONTROLLER, "motor_steps": [2400, 800]}
        self.plotter.remember_home(OPTIONS, {"firmware": "regression fixture"}, away)
        with patch.object(plotter, "controller_state", side_effect=[away, away, away, CONTROLLER]):
            prepared = self.plotter.prepare(plotter.DEVICE_RUN_ID, plotter.MOTION_TEST_SVG, OPTIONS)
            job = self.plotter.start(plotter.DEVICE_RUN_ID, plotter.MOTION_TEST_SVG,
                                     prepared["token"], True, "home", homed=True)
            self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(result["home"]["from_mm"], [20, 10])
        self.assertEqual([c["manual_cmd"] for c in self.driver.calls], ["raise_pen", "walk_home"])
        self.assertTrue(all(c["mode"] == "manual" for c in self.driver.calls))
        self.assertTrue(self.plotter.snapshot()["home_ready"])
        receipt = Path(self.temp.name) / plotter.DEVICE_RUN_ID / "plots" / (job["id"] + ".json")
        self.assertEqual(json.loads(receipt.read_text())["action"], "home")

    def test_device_preflight_cannot_be_used_to_plot_calibration_geometry(self):
        prepared = self.plotter.prepare(plotter.DEVICE_RUN_ID, plotter.MOTION_TEST_SVG, OPTIONS)
        with self.assertRaisesRegex(ValueError, "Select a saved drawing"):
            self.plotter.start(plotter.DEVICE_RUN_ID, plotter.MOTION_TEST_SVG, prepared["token"], True, homed=True)
        self.assertIsNone(self.plotter.worker)
        self.assertEqual(self.plotter.prepared["token"], prepared["token"])

    def test_failed_pen_lift_never_sends_home_or_drawing_commands(self):
        for changed in ({"pen_reported_up": False}, {"servo_power_on": False},
                        {"motor_steps": [1, 1]}, {"motor_microsteps": [8, 8]}, {"queue_idle": False}):
            self.driver = Driver()
            prepared = self.prepare()
            with patch.object(plotter, "controller_state", side_effect=[CONTROLLER, {**CONTROLLER, **changed}]):
                self.start(prepared)
                self.plotter.worker.join(3)
            self.assertEqual(self.plotter.snapshot()["job"]["state"], "error")
            self.assertEqual([c["manual_cmd"] for c in self.driver.calls], ["raise_pen"])
            self.assertIsNone(self.plotter.home)

    def test_failed_home_return_never_starts_sketch(self):
        for changed in ({"motor_steps": [1, 0]}, {"motor_microsteps": [0, 0]},
                        {"pen_reported_up": False}, {"servo_power_on": False}, {"queue_idle": False}):
            self.driver = Driver()
            prepared = self.prepare()
            with patch.object(plotter, "controller_state", side_effect=[CONTROLLER, CONTROLLER, {**CONTROLLER, **changed}]):
                self.start(prepared)
                self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(result["state"], "error", result)
            self.assertFalse(result["home"]["controller_verified"])
            self.assertEqual([c["manual_cmd"] for c in self.driver.calls], ["raise_pen", "walk_home"])
            self.assertIsNone(self.plotter.home)

    def test_reference_changes_between_preflight_and_start_block_all_motion(self):
        for changed in ({"motor_steps": [1, 0]}, {"motor_microsteps": [8, 8]}, {"queue_idle": False}):
            self.driver = Driver()
            prepared = self.prepare()
            with patch.object(plotter, "controller_state", return_value={**CONTROLLER, **changed}):
                self.start(prepared)
                self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(result["state"], "error", result)
            self.assertFalse(result["physical_motion_attempted"])
            self.assertEqual(self.driver.calls, [])

    def test_home_return_rejects_out_of_range_counters(self):
        for steps in ([40000, 40000], [-800, -800], [40000, -40000]):
            self.driver = Driver()
            away = {**CONTROLLER, "motor_steps": steps}
            self.plotter.remember_home(OPTIONS, {"firmware": "regression fixture"}, away)
            with patch.object(plotter, "controller_state", return_value=away):
                self.start(self.prepare(home=False))
                self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(result["state"], "error", result)
            self.assertIn("travel envelope", result["error"])
            self.assertEqual([c["manual_cmd"] for c in self.driver.calls], ["raise_pen"])

    def test_clean_pause_retains_position_for_the_next_home_return(self):
        prepared = self.prepare()
        original_run = self.driver.plot_run
        def pause_drawing():
            original_run()
            if self.driver.options.mode == "plot":
                self.driver.errors.code = 102
        self.driver.plot_run = pause_drawing
        away = {**CONTROLLER, "motor_steps": [2400, 800]}
        with patch.object(plotter, "controller_state", side_effect=[CONTROLLER, CONTROLLER, CONTROLLER, away]):
            self.start(prepared)
            self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "paused", result)
        self.assertTrue(result["home_reference_retained"])
        self.assertEqual(self.plotter.home["motor_steps"], [2400, 800])
        self.driver = Driver()
        with patch.object(plotter, "controller_state", side_effect=[away, away, away, CONTROLLER, CONTROLLER]):
            self.start(self.prepare(home=False))
            self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(result["home"]["from_mm"], [20, 10])

    def test_pause_during_home_never_starts_drawing(self):
        prepared = self.prepare()
        original_run = self.driver.plot_run
        def pause_home():
            original_run()
            if self.driver.options.manual_cmd == "walk_home":
                self.driver.errors.code = 103
        self.driver.plot_run = pause_home
        self.start(prepared)
        self.plotter.worker.join(3)
        self.assertEqual(self.plotter.snapshot()["job"]["state"], "paused")
        self.assertEqual([c["manual_cmd"] for c in self.driver.calls], ["raise_pen", "walk_home"])

    def test_preflight_invalidates_home_on_controller_change_or_connection_failure(self):
        for changed in ({"motor_steps": [10, 10]}, {"motor_microsteps": [0, 0]}, {"motor_microsteps": [8, 8]}):
            self.prepare()
            with patch.object(plotter, "controller_state", return_value={**CONTROLLER, **changed}):
                self.assertFalse(self.prepare(home=False)["home_ready"])
            self.assertIsNone(self.plotter.home)
        self.prepare()
        with patch.object(plotter, "checked_connection", side_effect=plotter.PlotterCommunicationError("USB timeout")):
            with self.assertRaises(plotter.PlotterCommunicationError):
                self.prepare(home=False)
        self.assertIsNone(self.plotter.home)

    def test_usb_failure_during_home_never_sends_a_drawing(self):
        prepared = self.prepare()
        original_run = self.driver.plot_run
        def fail_home():
            original_run()
            if self.driver.options.manual_cmd == "walk_home":
                raise plotter.PlotterCommunicationError("USB timeout")
        self.driver.plot_run = fail_home
        self.start(prepared)
        self.plotter.worker.join(3)
        self.assertEqual(self.plotter.snapshot()["job"]["state"], "error")
        self.assertEqual([c["manual_cmd"] for c in self.driver.calls], ["raise_pen", "walk_home"])
        self.assertIsNone(self.plotter.home)

    def test_set_home_rejects_nonzero_counters_after_motor_enable(self):
        released = {**CONTROLLER, "motor_microsteps": [0, 0], "motors_enabled": False}
        with patch.object(plotter, "controller_state", side_effect=[released, released, released,
                                                                   {**CONTROLLER, "motor_steps": [1, 0]}]):
            prepared = self.prepare(home=False)
            self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, "set_home", homed=True)
            self.plotter.worker.join(3)
        self.assertEqual(self.plotter.snapshot()["job"]["state"], "error")
        self.assertIsNone(self.plotter.home)

    def test_confirmation_and_unchanged_drawing_are_required(self):
        prepared = self.prepare()
        for confirmed in (False, 1, "true", None):
            with self.assertRaises(ValueError):
                self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], confirmed)
        with self.assertRaises(ValueError):
            self.plotter.start("aaaaaaaaaaaa", SVG + " ", prepared["token"], True)
        with self.assertRaises(ValueError):
            self.plotter.start("bbbbbbbbbbbb", SVG, prepared["token"], True)
        plotter.make_driver.assert_not_called()

    def test_expired_token_cannot_start(self):
        prepared = self.prepare()
        self.plotter.prepared["expires"] = time.monotonic() - 1
        with self.assertRaises(ValueError):
            self.start(prepared)

    def test_calibration_actions_use_fixed_modes_and_require_confirmation(self):
        for action in ("pen_up", "pen_cycle", "motion_test"):
            prepared = self.prepare()
            with self.assertRaises(ValueError):
                self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], False, action, True)
            if action == "motion_test":
                with self.assertRaises(ValueError):
                    self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, action)
            self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, action, True, homed=True)
            self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(result["state"], "complete", result)
            self.assertFalse(result["physical_output_verified"])
            if action == "pen_up":
                self.assertEqual(self.driver.options.mode, "manual")
                self.assertEqual(self.driver.options.manual_cmd, "raise_pen")
            elif action == "pen_cycle":
                self.assertEqual(self.driver.options.mode, "cycle")
            else:
                self.assertEqual(plotter.make_driver.call_args.args[0], plotter.MOTION_TEST_SVG)
                self.assertEqual(self.driver.options.pen_pos_down, self.driver.options.pen_pos_up)
                self.assertEqual(self.driver.options.speed_penup, 10)
                self.assertEqual(result["test_envelope_mm"], [5, 5])
                self.assertEqual(result["effective_settings"]["accel"], 20)

    def test_plot_and_motion_test_require_explicit_physical_home_confirmation(self):
        prepared = self.prepare()
        for action in ("plot", "home", "motion_test"):
            for homed in (False, None, "true", 1):
                with self.assertRaisesRegex(ValueError, "Home corner"):
                    self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, action, True, homed=homed)
        self.assertIsNone(self.plotter.worker)

    def test_alignment_releases_motors_without_automatically_homing(self):
        for enabled in (True, False):
            prepared = self.prepare()
            with patch.object(plotter, "controller_state", side_effect=[CONTROLLER, {**CONTROLLER, "motors_enabled": enabled}]):
                self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, "align")
                self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(self.driver.options.mode, "align")
            self.assertEqual(result["state"], "error" if enabled else "complete")
            self.assertFalse(result["operator_confirmed_home"])
            self.assertFalse(result["physical_output_verified"])

    def test_calibration_rejects_changed_counters_or_pen_still_down(self):
        for after in ({**CONTROLLER, "motor_steps": [101, 201]}, {**CONTROLLER, "pen_reported_up": False}):
            prepared = self.prepare()
            with patch.object(plotter, "controller_state", side_effect=[CONTROLLER, after]):
                self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, "pen_up")
                self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(result["state"], "error")
            self.assertFalse(result["driver_reported_complete"])

    def test_unknown_calibration_action_does_not_consume_preflight(self):
        prepared = self.prepare()
        with self.assertRaises(ValueError):
            self.plotter.start("aaaaaaaaaaaa", SVG, prepared["token"], True, "unknown_action")
        self.assertEqual(self.plotter.prepared["token"], prepared["token"])

    def test_shutdown_invalidates_preflight_and_prevents_new_motion(self):
        prepared = self.prepare()
        self.plotter.close()
        with self.assertRaises(RuntimeError):
            self.start(prepared)
        self.assertIsNone(self.plotter.worker)

    def test_token_single_use_and_completion_is_not_physical_verification(self):
        prepared = self.prepare()
        job = self.start(prepared)
        self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "complete")
        self.assertTrue(result["driver_reported_complete"])
        self.assertFalse(result["physical_output_verified"])
        receipt = Path(self.temp.name) / "aaaaaaaaaaaa" / "plots" / (job["id"] + ".json")
        self.assertEqual(json.loads(receipt.read_text()), result)
        with self.assertRaises(ValueError):
            self.start(prepared)

    def test_driver_errors_and_zero_distance_never_report_success(self):
        for code, state in ((101, "error"), (104, "error"), (102, "paused"), (103, "paused")):
            self.driver = Driver(code=code)
            prepared = self.prepare()
            self.start(prepared)
            self.plotter.worker.join(3)
            result = self.plotter.snapshot()["job"]
            self.assertEqual(result["state"], state)
            self.assertFalse(result["driver_reported_complete"])
        for driver in (Driver(distance=0), Driver(warnings=["Missing motor power"])):
            self.driver = driver
            self.start(self.prepare())
            self.plotter.worker.join(3)
            self.assertEqual(self.plotter.snapshot()["job"]["state"], "error")

    def test_busy_and_stale_pause_requests_are_rejected(self):
        self.driver.wait = True
        prepared = self.prepare()
        job = self.start(prepared)
        self.assertTrue(self.driver.entered.wait(2))
        with self.assertRaises(RuntimeError):
            self.prepare()
        with self.assertRaises(RuntimeError):
            self.start(prepared)
        with self.assertRaises(ValueError):
            self.plotter.pause("other-job")
        self.plotter.pause(job["id"])
        self.plotter.worker.join(3)
        self.assertEqual(self.plotter.snapshot()["job"]["state"], "paused")

    def test_disconnect_between_preflight_and_start_fails_before_movement(self):
        prepared = self.prepare()
        with patch.object(plotter, "checked_connection", side_effect=RuntimeError("USB disconnected")):
            self.start(prepared)
            self.plotter.worker.join(3)
        result = self.plotter.snapshot()["job"]
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["physical_motion_attempted"])


@unittest.skipUnless(importlib.util.find_spec("pyaxidraw"), "Optional official AxiDraw SDK not installed")
class OfficialPreviewTests(unittest.TestCase):
    def test_official_walk_home_emits_only_pen_up_return_steps_without_resetting_origin(self):
        from plotink import ebb_motion, ebb_serial
        replies = {"QC": "0,300", "QL": "31", "QP": "1", "QG": "00", "QB": "0",
                   "QS": "2400,800", "PI,E,0": "PI,0", "PI,C,1": "PI,0",
                   "PI,E,2": "PI,1", "PI,E,1": "PI,1", "PI,A,6": "PI,1"}
        settings = plotter.settings_for(OPTIONS)
        with patch.object(ebb_serial, "testPort", side_effect=AssertionError("No physical USB in tests")), \
                patch.object(ebb_serial, "openPort", side_effect=AssertionError("No physical USB in tests")), \
                patch.object(ebb_serial, "queryVersion", return_value="EBB Firmware Version 2.8.1"), \
                patch.object(ebb_serial, "query", side_effect=lambda _, command, *args: replies[command.strip()]), \
                patch.object(ebb_serial, "command") as commands, \
                patch.object(ebb_motion, "doXYMove") as moves, \
                patch.object(ebb_motion, "sendPenDown", side_effect=AssertionError("Pen down during Home return")):
            for command in ("raise_pen", "walk_home"):
                driver = plotter.make_driver(SVG, settings, preview=False)
                driver.options.port = object()
                driver.options.mode = "manual"
                driver.options.manual_cmd = command
                driver.plot_run()
                self.assertEqual(driver.errors.code, 0)
                self.assertEqual(plotter.driver_warnings(driver), [])
                if command == "raise_pen":
                    moves.assert_not_called()
            self.assertGreater(moves.call_count, 0)
            self.assertEqual(sum(call.args[1] for call in moves.call_args_list), -800)
            self.assertEqual(sum(call.args[2] for call in moves.call_args_list), -2400)
            self.assertFalse(any(call.args[1].startswith("EM,") for call in commands.call_args_list))

    def test_real_sdk_missing_reply_escapes_legacy_retry_bug(self):
        from plotink import ebb_serial
        raw = SimpleNamespace(write=Mock(return_value=3), readline=Mock(return_value=b""))
        connection = plotter.LoggedConnection(raw, timeout=0.01)
        with self.assertRaisesRegex(plotter.PlotterCommunicationError, "after QS"):
            ebb_serial.query(connection, "QS\r")
        raw.write.assert_called_once_with(b"QS\r")

    def test_real_driver_preview_and_pause_event_never_open_usb(self):
        from plotink import ebb_serial
        settings = plotter.settings_for(OPTIONS)
        with patch.object(ebb_serial, "testPort", side_effect=AssertionError("USB access in preview")), \
                patch.object(ebb_serial, "openPort", side_effect=AssertionError("USB access in preview")):
            preview = plotter.preview_svg(SVG, settings)
            self.assertGreater(preview["estimated_seconds"], 0)
            stop = threading.Event()
            stop.set()
            driver = plotter.make_driver(SVG, settings, preview=True, stop=stop)
            driver.plot_run()
            self.assertTrue(driver.receive_pause_request())
            self.assertEqual(driver.errors.code, 103)
            driver = plotter.make_driver(SVG, settings, preview=False, stop=stop)
            self.assertFalse(driver.options.preview)
            self.assertFalse(driver.options.auto_rotate)
            self.assertEqual(driver.options.resolution, 1)
            self.assertEqual(driver.options.reordering, 4)


if __name__ == "__main__":
    unittest.main()
