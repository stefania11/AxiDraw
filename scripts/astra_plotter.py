"""Guarded AxiDraw execution; discovery and preflight never command movement."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from svg_geometry import PAGE_HEIGHT_MM, PAGE_WIDTH_MM

ACTIVE = {"checking", "running", "pausing"}
MODELS = {1: "V2 / V3 / SE-A4", 2: "V3/A3 / SE-A3", 3: "V3 XLX", 5: "SE/A1", 6: "SE/A2"}
TEST_ACTIONS = {"align", "set_home", "home", "pen_up", "pen_cycle", "motion_test"}
DEVICE_RUN_ID = "plotter"
MOTION_TEST_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" '
                   'viewBox="0 0 297 210"><polyline points="0,0 5,0 5,5 0,5 0,0" '
                   'fill="none" stroke="black"/></svg>')


class PlotterCommunicationError(Exception):
    """Not a RuntimeError: the legacy SDK catches and suppresses that exception."""


class LoggedConnection:
    def __init__(self, connection, timeout=3.0):
        self.connection = connection
        self.connection.timeout = 0.2
        self.connection.write_timeout = 1.0
        self.timeout = timeout
        self.started = time.monotonic()
        self.deadline = self.started
        self.last_command = None
        self.last_response = None
        self.events = deque(maxlen=256)

    def log(self, direction, data):
        self.events.append({"ms": round((time.monotonic() - self.started) * 1000),
                            "direction": direction, "data": data.decode("ascii", errors="backslashreplace")})

    def write(self, data):
        self.last_command = data.decode("ascii", errors="backslashreplace").strip()
        self.log("tx", data)
        self.deadline = time.monotonic() + self.timeout
        try:
            return self.connection.write(data)
        except (OSError, RuntimeError) as exc:
            raise PlotterCommunicationError(f"USB write failed for {self.last_command}: {exc}") from exc

    def readline(self):
        data = b""
        try:
            while time.monotonic() < self.deadline:
                data += self.connection.readline()
                if data.endswith(b"\n"):
                    self.log("rx", data)
                    self.last_response = data.decode("ascii", errors="backslashreplace").strip()
                    if b"Err:" in data:
                        raise PlotterCommunicationError(f"Controller rejected {self.last_command}: {self.last_response}")
                    return data
        except (OSError, RuntimeError) as exc:
            raise PlotterCommunicationError(f"USB read failed after {self.last_command}: {exc}") from exc
        self.log("rx_timeout", data)
        raise PlotterCommunicationError(
            f"No complete USB reply within {self.timeout:g} seconds after {self.last_command}. "
            "No further commands were sent; use the physical pause button and inspect the machine."
        )

    def close(self):
        self.connection.close()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def discover():
    try:
        from pyaxidraw import axidraw
        from plotink import ebb_serial
        devices = [{"port": p[0], "label": p[1]} for p in ebb_serial.listEBBports() or []]
        return {"available": True, "devices": devices,
                "message": "Choose your AxiDraw." if devices else "No AxiDraw USB controller detected."}
    except (ImportError, OSError) as exc:
        return {"available": False, "devices": [], "message": f"AxiDraw driver unavailable: {exc}"}


def settings_for(data):
    if not isinstance(data, dict):
        raise ValueError("Plot settings are required.")
    result = {}
    for key, default, low, high in (("model", 1, 1, 6), ("speed_pendown", 25, 1, 50),
                                  ("speed_penup", 30, 1, 75), ("accel", 25, 1, 75),
                                  ("pen_pos_up", 60, 0, 100), ("pen_pos_down", 30, 0, 100)):
        value = data.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}.")
        result[key] = value
    if result["model"] not in MODELS:
        raise ValueError("This A4 drawing needs an A4-sized or larger AxiDraw.")
    if result["pen_pos_up"] <= result["pen_pos_down"]:
        raise ValueError("Pen-up height must be greater than pen-down height.")
    port = data.get("port")
    if not isinstance(port, str) or not port or len(port) > 256:
        raise ValueError("Select an AxiDraw USB device.")
    result["port"] = port
    return result


@contextmanager
def device_lock():
    # Shared by all Astra studio instances, held until USB cleanup completes.
    path = Path("/tmp") / f"astra-axidraw-{os.getuid()}.lock"
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Astra studio owns the plotter.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def checked_connection(port):
    from plotink import ebb_serial
    if port not in {d["port"] for d in discover()["devices"]}:
        raise RuntimeError("The selected AxiDraw is disconnected. Check USB and power.")
    connection = ebb_serial.testPort(port)
    if connection is None:
        raise RuntimeError("Could not open AxiDraw. Close other plotter apps and check USB.")
    connection = LoggedConnection(connection)
    try:
        if not ebb_serial.min_version(connection, "2.6.2"):
            raise RuntimeError("Firmware 2.6.2 or newer is required for the idle check.")
        firmware = ebb_serial.queryVersion(connection).strip()
        power = ebb_serial.query(connection, "QC\r").strip()
        # Match the driver's 250-count motor-supply threshold, but fail on missing data.
        if not re.fullmatch(r"[0-9]+,[0-9]+", power) or int(power.split(",")[1]) < 250:
            raise RuntimeError("Motor power is absent or could not be verified. Check the power adapter.")
        status = ebb_serial.query(connection, "QG\r").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{1,2}", status) or int(status, 16) & 15:
            raise RuntimeError("AxiDraw is busy or its idle state could not be verified.")
        yield connection, {"firmware": firmware, "motor_power_detected": True, "idle": True,
                           "power_adc_counts": int(power.split(",")[1]), "status_hex": status}
    finally:
        ebb_serial.closePort(connection)


def make_driver(svg, settings, *, preview, stop=None):
    try:
        from pyaxidraw import axidraw
    except ImportError as exc:
        raise RuntimeError("Install the official AxiDraw Python API in the server environment.") from exc

    class PausableAxiDraw(axidraw.AxiDraw):
        def set_up_pause_transmitter(self):
            super().set_up_pause_transmitter()
            if stop is not None:
                # The SDK resets its event in plot_run; retain early UI pause requests.
                self.software_initiated_pause_event = stop

    driver = PausableAxiDraw()
    driver.plot_setup(svg)
    for key, value in settings.items():
        if key != "port":
            setattr(driver.options, key, value)
    driver.options.preview = preview
    driver.options.mode = "plot"
    driver.options.auto_rotate = False
    driver.options.resolution = 1
    driver.options.reordering = 4
    driver.options.const_speed = False
    driver.options.copies = 1
    driver.options.rendering = 0
    driver.options.webhook = False
    driver.options.port_config = 0
    driver.options.pen_rate_raise = 30
    driver.options.pen_rate_lower = 30
    driver.params.skip_voltage_check = False
    return driver


def driver_warnings(driver):
    return driver.warnings.return_text_list()


def controller_state(connection):
    from plotink import ebb_serial
    values = {command: ebb_serial.query(connection, command + "\r").strip()
              for command in ("QS", "QP", "QR", "QM", "QE")}
    if (not re.fullmatch(r"-?[0-9]+,-?[0-9]+", values["QS"]) or
            values["QP"] not in {"0", "1"} or values["QR"] not in {"0", "1"} or
            not re.fullmatch(r"QM,[0-9],[01],[01],[01]", values["QM"]) or
            not re.fullmatch(r"(?:0|1|2|4|8|16),(?:0|1|2|4|8|16)", values["QE"])):
        raise PlotterCommunicationError("Malformed controller-state reply: " + repr(values))
    return {"raw": values, "motor_steps": [int(n) for n in values["QS"].split(",")],
            "motor_microsteps": [int(n) for n in values["QE"].split(",")],
            "pen_reported_up": values["QP"] == "1", "servo_power_on": values["QR"] == "1",
            "queue_idle": values["QM"] == "QM,0,0,0,0", "motors_enabled": values["QE"] != "0,0"}


def wait_idle(connection, timeout=10):
    from plotink import ebb_serial
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = ebb_serial.query(connection, "QM\r").strip()
        if reply == "QM,0,0,0,0":
            return
        if not re.fullmatch(r"QM,[0-9],[01],[01],[01]", reply):
            raise PlotterCommunicationError("Malformed idle reply: " + repr(reply))
        time.sleep(0.05)
    raise PlotterCommunicationError("Motion queue did not become idle. Use the physical pause button.")


def home_position(controller, driver):
    # Same motor-to-XY conversion as the SDK's walk_home, in high-resolution mode.
    a, b = controller["motor_steps"]
    factor = 25.4 / (4 * driver.params.native_res_factor)
    position = [(a + b) * factor, (a - b) * factor]
    if (controller["motor_microsteps"] != [16, 16] or
            any(not math.isfinite(p) or p < -0.02 or p > edge + 0.02
                for p, edge in zip(position, (PAGE_WIDTH_MM, PAGE_HEIGHT_MM)))):
        raise RuntimeError("Home return exceeds the A4 travel envelope or resolution changed. Release XY and set Home again.")
    return position


def preview_svg(svg, settings, stop=None):
    if stop is not None and stop.is_set():
        raise InterruptedError("Plot cancelled before movement.")
    driver = make_driver(svg, settings, preview=True, stop=stop)
    driver.plot_run()
    if stop is not None and stop.is_set():
        raise InterruptedError("Plot cancelled before movement.")
    warnings = driver_warnings(driver)
    if driver.errors.code or driver.plot_status.stopped or warnings:
        raise RuntimeError("AxiDraw preflight failed: " + (" ".join(warnings) or "driver interrupted"))
    if not math.isfinite(driver.distance_pendown) or driver.distance_pendown <= 0:
        raise RuntimeError("AxiDraw preflight found no drawable geometry.")
    return {"estimated_seconds": round(driver.time_estimate, 1),
            "pen_down_m": round(driver.distance_pendown, 4), "warnings": warnings}


class Plotter:
    def __init__(self, outputs, enabled=False):
        self.outputs = outputs
        self.enabled = enabled
        self.lock = threading.Lock()
        self.job = {"state": "idle"}
        self.prepared = None
        self.stop = threading.Event()
        self.worker = None
        self.connection = None
        self.home = None

    def home_matches(self, settings, device, controller):
        return bool(self.home and self.home == {
            "port": settings["port"], "model": settings["model"], "firmware": device["firmware"],
            "motor_steps": controller["motor_steps"],
        } and controller["motor_microsteps"] == [16, 16] and controller["queue_idle"])

    def remember_home(self, settings, device, controller):
        self.home = {"port": settings["port"], "model": settings["model"],
                     "firmware": device["firmware"], "motor_steps": list(controller["motor_steps"])}

    def snapshot(self):
        with self.lock:
            job = dict(self.job)
            if self.connection is not None and job["state"] in ACTIVE:
                job["last_command"] = self.connection.last_command
                job["last_response"] = self.connection.last_response
            return {"enabled": self.enabled, "job": job, "home_ready": self.home is not None}

    def prepare(self, run_id, svg, options):
        if not self.enabled:
            raise RuntimeError("Physical plotting is disabled. Start the server with --enable-plotter.")
        settings = settings_for(options)
        with self.lock:
            if self.job["state"] in ACTIVE:
                raise RuntimeError("The plotter is already busy.")
            self.prepared = None
            self.job = {"state": "checking", "run_id": run_id}
        try:
            with device_lock():
                preview = preview_svg(svg, settings)
                with checked_connection(settings["port"]) as (connection, device):
                    device["controller"] = controller_state(connection)
                    home_ready = self.home_matches(settings, device, device["controller"])
                    if not home_ready:
                        self.home = None
            prepared = {"token": uuid.uuid4().hex, "run_id": run_id, "svg": svg,
                        "svg_sha256": hashlib.sha256(svg.encode()).hexdigest(),
                        "settings": settings, "preview": preview, "device": device, "home_ready": home_ready,
                        "expires": time.monotonic() + 120}
            with self.lock:
                self.prepared = prepared
                self.job = {"state": "idle"}
            return {k: v for k, v in prepared.items() if k not in {"svg", "expires"}}
        except Exception:
            with self.lock:
                self.home = None
                self.job = {"state": "idle"}
            raise

    def start(self, run_id, svg, token, confirmed, action="plot", pen_clear=False, homed=False):
        if not isinstance(action, str) or action not in {"plot", *TEST_ACTIONS}:
            raise ValueError("Unknown plotter action.")
        if action == "plot" and run_id == DEVICE_RUN_ID:
            raise ValueError("Select a saved drawing before plotting.")
        if action == "motion_test" and pen_clear is not True:
            raise ValueError("Confirm the pen physically clears the paper and 5 mm of travel is unobstructed.")
        if confirmed is not True:
            raise ValueError("Confirm the power adapter, secured pen, and a clear workspace first.")
        if action in {"plot", "home", "motion_test", "set_home"} and homed is not True:
            raise ValueError("Confirm the physical Home corner, aligned A4 paper, and unchanged carriage setup first.")
        with self.lock:
            if not self.enabled or self.job["state"] in ACTIVE:
                raise RuntimeError("Physical plotting is disabled or already busy.")
            prepared = self.prepared
            if not prepared or token != prepared["token"] or prepared["expires"] < time.monotonic():
                raise ValueError("Preflight expired or was already used. Check the plotter again.")
            if run_id != prepared["run_id"] or svg != prepared["svg"]:
                raise ValueError("The drawing changed. Run preflight again.")
            if action in {"plot", "home", "motion_test"} and (not prepared["home_ready"] or self.home is None):
                raise ValueError("Set Home first: release XY, place the head at the physical Home corner, then choose Set Home.")
            self.prepared = None
            self.stop = threading.Event()
            self.job = {"state": "running", "id": uuid.uuid4().hex[:12], "run_id": run_id, "action": action,
                        "started_at": time.time(), "physical_motion_attempted": False}
            self.worker = threading.Thread(target=self._run, args=({**prepared, "action": action, "homed": homed},), daemon=False)
            self.worker.start()
            return dict(self.job)

    def pause(self, job_id):
        with self.lock:
            if self.job.get("id") != job_id or self.job["state"] not in {"running", "pausing"}:
                raise ValueError("There is no matching active plot to pause.")
            self.stop.set()
            self.job["state"] = "pausing"
            return dict(self.job)

    def _run(self, prepared):
        result = dict(self.job)
        result.update(svg_sha256=prepared["svg_sha256"], settings=prepared["settings"],
                      preflight=prepared["preview"], operator_confirmed_ready=True,
                      operator_confirmed_home=prepared.get("homed") is True,
                      physical_output_verified=False, driver_reported_complete=False)
        driver = None
        connection = None
        action = prepared.get("action", "plot")
        try:
            with device_lock():
                if self.stop.is_set():
                    raise InterruptedError("Plot cancelled before movement.")
                preview_svg(prepared["svg"], prepared["settings"], self.stop)
                with checked_connection(prepared["settings"]["port"]) as (connection, device):
                    with self.lock:
                        self.connection = connection if isinstance(connection, LoggedConnection) else None
                    result["device"] = device
                    result["controller_before"] = controller_state(connection)
                    if action in {"plot", "home", "motion_test"} and not self.home_matches(
                            prepared["settings"], device, result["controller_before"]):
                        raise RuntimeError("Home reference changed since preflight. Release XY and set Home again.")
                    if action in {"align", "set_home"}:
                        self.home = None
                    svg = MOTION_TEST_SVG if action == "motion_test" else prepared["svg"]
                    settings = dict(prepared["settings"])
                    if action == "motion_test":
                        settings.update(pen_pos_down=settings["pen_pos_up"], speed_pendown=10,
                                        speed_penup=10, accel=20)
                        result.update(test_envelope_mm=[5, 5], operator_confirmed_pen_clear=True,
                                      executed_svg_sha256=hashlib.sha256(svg.encode()).hexdigest())
                    result["effective_settings"] = settings

                    def execute(mode="plot", command=None, phase="drawing"):
                        nonlocal driver
                        stage_settings = dict(settings)
                        if command == "walk_home":
                            stage_settings.update(speed_penup=min(settings["speed_penup"], 15),
                                                  accel=min(settings["accel"], 20))
                        driver = make_driver(svg, stage_settings, preview=False, stop=self.stop)
                        driver.options.port = connection
                        driver.options.mode = mode
                        if command:
                            driver.options.manual_cmd = command
                        if command == "walk_home":
                            # The SDK walk ignores travel limits; bound its counter-derived move first.
                            result["home"]["from_mm"] = home_position(result["home"]["after_pen_up"], driver)
                        if self.stop.is_set():
                            raise InterruptedError("Plot cancelled before the next movement.")
                        result.update(physical_motion_attempted=True, phase=phase)
                        with self.lock:
                            self.job.update(physical_motion_attempted=True, phase=phase)
                        driver.plot_run()
                        code = driver.errors.code or driver.plot_status.stopped
                        result["driver_error_code"] = code
                        paused = code in {102, 103, -103}
                        if code and not paused:
                            raise RuntimeError(f"AxiDraw stopped with driver error {code}; inspect and set Home before retrying.")
                        warnings = driver_warnings(driver)
                        if warnings:
                            raise RuntimeError("AxiDraw reported: " + " ".join(warnings))
                        wait_idle(connection, timeout=60 if command == "walk_home" else 10)
                        after_stage = controller_state(connection)
                        if paused or self.stop.is_set():
                            result["controller_after"] = after_stage
                            if (self.home and after_stage["queue_idle"] and after_stage["pen_reported_up"] and
                                    after_stage["servo_power_on"] and (phase in {"drawing", "returning_home"} or
                                    self.home_matches(settings, device, after_stage))):
                                home_position(after_stage, driver)
                                self.remember_home(settings, device, after_stage)
                                result["home_reference_retained"] = True
                                raise InterruptedError("Plot paused with a tracked position. The next sketch will return Home first; reset Home after any collision or manual movement.")
                            raise InterruptedError("Plot paused. Release XY and set Home before another plot.")
                        return after_stage

                    if action in {"plot", "home", "motion_test", "set_home"}:
                        result["home"] = {"controller_verified": False, "physical_position_verified": False}
                        reference = result["controller_before"]
                        if action == "set_home" and reference["motors_enabled"]:
                            released = execute("align", None, "releasing_xy")
                            result["home"]["after_release"] = released
                            if (released["motors_enabled"] or released["motor_microsteps"] != [0, 0] or
                                    not released["pen_reported_up"] or not released["queue_idle"]):
                                raise RuntimeError("XY release could not be verified. Home was not set.")
                            reference = released
                        if action == "set_home" and reference["motor_microsteps"] != [0, 0]:
                            raise RuntimeError("XY release could not be verified. Home was not set.")
                        raised = execute("manual", "raise_pen", "raising_pen")
                        result["home"]["after_pen_up"] = raised
                        if (not raised["pen_reported_up"] or not raised["servo_power_on"] or
                                not raised["queue_idle"] or raised["motor_steps"] != reference["motor_steps"] or
                                raised["motor_microsteps"] != reference["motor_microsteps"]):
                            raise RuntimeError("Pen lift or stationary carriage could not be verified. No Home move was sent.")
                        command = "enable_xy" if action == "set_home" else "walk_home"
                        at_home = execute("manual", command, "setting_home" if action == "set_home" else "returning_home")
                        result["home"].update(command=command, controller_after=at_home)
                        if (at_home["motor_steps"] != [0, 0] or at_home["motor_microsteps"] != [16, 16] or
                                not at_home["queue_idle"] or not at_home["pen_reported_up"] or not at_home["servo_power_on"]):
                            raise RuntimeError("Home return was not verified by the controller. The sketch was not started.")
                        result["home"]["controller_verified"] = True
                        if action in {"home", "set_home"}:
                            result["controller_after"] = at_home
                        else:
                            result["controller_after"] = execute()
                    else:
                        mode, command = {"align": ("align", None), "pen_up": ("manual", "raise_pen"),
                                         "pen_cycle": ("cycle", None)}[action]
                        result["controller_after"] = execute(mode, command, "calibrating")
                    if action == "plot" and (not math.isfinite(driver.distance_pendown) or driver.distance_pendown <= 0):
                        raise RuntimeError("Driver reported no pen-down movement.")
                    after = result["controller_after"]
                    if action in {"plot", "home", "motion_test"} and (after["motor_steps"] != [0, 0] or
                            after["motor_microsteps"] != [16, 16] or not after["pen_reported_up"] or
                            not after["queue_idle"] or not after["servo_power_on"]):
                        raise RuntimeError("Plot did not finish at Home with the pen raised. Set Home before another plot.")
                    if action in TEST_ACTIONS:
                        before = [0, 0] if action == "motion_test" else result["controller_before"]["motor_steps"]
                        if action not in {"align", "set_home", "home"} and before != after["motor_steps"]:
                            raise RuntimeError("Calibration did not return the motor counters to their starting values.")
                        if action == "align" and result["controller_after"]["motors_enabled"]:
                            raise RuntimeError("XY motors did not report disabled; do not move the carriage by hand.")
                        if not result["controller_after"]["pen_reported_up"]:
                            raise RuntimeError("Controller did not report pen up after the test.")
                    message = {
                        "plot": "Driver completed. Inspect the paper to verify the physical result.",
                        "home": "Returned Home with the pen raised. Check the physical position.",
                        "set_home": "Pen raised, XY released if needed, and the current position set as Home. Future sketches will return here before drawing.",
                        "align": "Pen raised; XY motors released. Gently move the solid carriage block fully left and back to Home, then align paper.",
                        "pen_up": "Pen-up command completed. Check that the tip clears the paper.",
                        "pen_cycle": "Pen down/up test completed. Check contact and clearance.",
                        "motion_test": "5 mm pen-up square completed; motor counters returned. Check smoothness and distance.",
                    }[action]
                    if action in {"plot", "home", "motion_test", "set_home"}:
                        self.remember_home(settings, device, after)
                    result.update(state="complete", driver_reported_complete=True,
                                  driver_elapsed_seconds=round(driver.time_estimate, 1),
                                  driver_pen_down_m=round(driver.distance_pendown, 4),
                                  message=message)
        except InterruptedError as exc:
            if not result.get("home_reference_retained"):
                self.home = None
            result.update(state="paused", message=str(exc))
        except Exception as exc:
            self.home = None
            result.update(state="error", error=str(exc))
        finally:
            result["finished_at"] = time.time()
            if driver is not None:
                result["driver_error_code"] = driver.errors.code or driver.plot_status.stopped
            if isinstance(connection, LoggedConnection):
                result["serial_trace"] = list(connection.events)
            try:
                folder = self.outputs / result["run_id"] / "plots"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f'{result["id"]}.json').write_text(json.dumps(result, indent=2) + "\n")
            except OSError:
                result["receipt_warning"] = "Could not save the plot receipt. Inspect the physical result before retrying."
            with self.lock:
                self.job = result
                self.connection = None

    def close(self):
        with self.lock:
            self.enabled = False
            self.prepared = None
            self.home = None
            self.stop.set()
            worker = self.worker
        if worker:
            worker.join()
