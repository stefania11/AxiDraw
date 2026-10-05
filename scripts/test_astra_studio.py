"""Geometry and local HTTP regression tests; these do not evaluate model quality."""

import base64
import copy
import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import astra_studio as studio
import astra_plotter as hardware


def geometry():
    return {"title": "test_curve", "description": "Test geometry", "strokes": [
        {"kind": "curve", "points": [[20, 20], [20, 80], [80, 80], [80, 20]]},
        {"kind": "polyline", "points": [[25, 30], [35, 40]]},
    ]}


def jpeg_data_url(size=(32, 24), image_format="JPEG"):
    data = io.BytesIO()
    Image.new("RGB", size, "white").save(data, format=image_format)
    return "data:image/jpeg;base64," + base64.b64encode(data.getvalue()).decode("ascii")


class PhotoTests(unittest.TestCase):
    def test_capture_bytes_and_dimensions_are_preserved(self):
        url = jpeg_data_url()
        photo = studio.decode_photo(url)
        self.assertEqual((photo.width, photo.height), (32, 24))
        self.assertEqual(photo.data, base64.b64decode(url.split(",", 1)[1]))
        self.assertEqual(photo.metadata()["sha256"], hashlib.sha256(photo.data).hexdigest())
        self.assertIsNone(studio.decode_photo(None))

    def test_invalid_images_are_rejected(self):
        for value in ("", 12, "https://example.com/image.jpg", "data:image/jpeg;base64,!!!",
                      "data:image/jpeg;base64,SGVsbG8=", jpeg_data_url(image_format="PNG"),
                      jpeg_data_url((1025, 8)), jpeg_data_url((8, 1025)),
                      "data:image/jpeg;base64," + "A" * studio.MAX_PHOTO_DATA_URL):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                studio.decode_photo(value)

    def test_photo_cannot_be_combined_with_a_plan_revision(self):
        with self.assertRaises(ValueError):
            studio.Studio("local").start("Draw the subject", "quick", "aaaaaaaaaaaa", jpeg_data_url())

    def test_local_transport_attaches_exact_photo(self):
        photo = studio.decode_photo(jpeg_data_url())

        def runtime_stub(command, **kwargs):
            image_file = Path(command[command.index("--image") + 1])
            self.assertEqual(image_file.read_bytes(), photo.data)
            self.assertEqual(command[command.index("--model") + 1], studio.MODEL)
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(geometry()))
            return subprocess.CompletedProcess(command, 0, stdout='{"type":"turn.completed","usage":{}}\n', stderr="")

        with tempfile.TemporaryDirectory() as folder, patch.object(studio, "runtime_binary", return_value="runtime"), \
                patch.object(studio.subprocess, "run", side_effect=runtime_stub), \
                patch.object(studio.subprocess, "check_output", return_value="test-runtime"):
            directory = Path(folder)
            _, receipt = studio.live_request("Draw the subject", directory, "local", "quick", photo=photo)
            request = json.loads((directory / "request.json").read_text())
            self.assertEqual(request["source_image"], photo.metadata())
            self.assertEqual(receipt["source_image"], photo.metadata())
            self.assertNotIn("base64", (directory / "request.json").read_text())

    def test_api_transport_sends_image_content(self):
        photo = studio.decode_photo(jpeg_data_url())

        def api_stub(request, **kwargs):
            body = json.loads(request.data)
            self.assertEqual(body["model"], studio.MODEL)
            self.assertFalse(body["store"])
            content = body["input"][0]["content"]
            self.assertEqual(content[0]["type"], "input_text")
            self.assertEqual(content[1]["type"], "input_image")
            self.assertEqual(base64.b64decode(content[1]["image_url"].split(",", 1)[1]), photo.data)
            return io.BytesIO(json.dumps({"status": "completed", "model": studio.MODEL,
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(geometry())}]}]}).encode())

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-placeholder"}), \
                patch.object(studio.urllib.request, "urlopen", side_effect=api_stub):
            _, receipt = studio.live_request("Draw the subject", Path(folder), "api", "quick", photo=photo)
            self.assertEqual(receipt["source_image"], photo.metadata())


class GeometryTests(unittest.TestCase):
    def test_curve_endpoints_and_sanitizer_limits(self):
        plan = studio.to_plan(geometry())
        self.assertEqual(plan.commands[0]["points"][0], [20, 20])
        self.assertEqual(plan.commands[0]["points"][-1], [80, 20])
        self.assertEqual(len(plan.commands[0]["points"]), 13)
        self.assertFalse(plan.warnings)

    def test_longest_curve_preserved(self):
        raw = geometry()
        raw["strokes"][0]["points"] = [[20 + i, 20 + i % 3] for i in range(16)]
        self.assertEqual(len(studio.to_plan(raw).commands[0]["points"]), 61)

    def test_nonfinite_and_outside_points_fail_before_clamping(self):
        for value in (float("nan"), float("inf"), True, "20", 9, 298):
            with self.subTest(value=value):
                raw = geometry()
                raw["strokes"][0]["points"][0][0] = value
                with self.assertRaises(ValueError):
                    studio.to_plan(raw)

    def test_invalid_curves_and_empty_geometry_fail(self):
        for strokes in ([], [{"kind": "curve", "points": [[20, 20], [30, 30]]}],
                        [{"kind": "polyline", "points": [[20, 20], [20, 20]]}],
                        [{"kind": "script", "points": [[20, 20], [30, 30]]}]):
            with self.subTest(strokes=strokes), self.assertRaises(ValueError):
                studio.to_plan({"strokes": strokes})

    def test_optimization_preserves_geometry_and_never_adds_travel(self):
        plan = studio.to_plan(geometry())
        original = copy.deepcopy(plan.commands)
        optimized = studio.optimize(plan)
        self.assertEqual(plan.commands, original)
        self.assertLessEqual(studio.pen_up_distance(optimized), studio.pen_up_distance(original))
        self.assertTrue(math.isclose(studio.estimate_pen_distance_mm(original),
                                     studio.estimate_pen_distance_mm(optimized), abs_tol=0.1))
        normalize = lambda c: min(tuple(map(tuple, c["points"])), tuple(map(tuple, reversed(c["points"]))))
        self.assertEqual(sorted(map(normalize, original)), sorted(map(normalize, optimized)))

    def test_centered_layout_is_smaller_uniform_and_does_not_modify_source(self):
        raw = geometry()
        original = copy.deepcopy(raw)
        plan = studio.to_plan(raw)
        commands = copy.deepcopy(plan.commands)
        centered = studio.centered_plan(plan)
        self.assertEqual(raw, original)
        self.assertEqual(plan.commands, commands)
        before = [p for c in plan.commands for p in c["points"]]
        after = [p for c in centered.commands for p in c["points"]]
        for axis, target in enumerate((148.5, 105)):
            low, high = min(p[axis] for p in after), max(p[axis] for p in after)
            self.assertAlmostEqual((low + high) / 2, target, delta=0.001)
            extent = max(p[axis] for p in before) - min(p[axis] for p in before)
            self.assertAlmostEqual(high - low, extent * 0.7, delta=0.001)
        self.assertEqual(len(centered.commands), len(plan.commands))
        for a, b in zip(plan.commands, centered.commands):
            self.assertEqual(len(a["points"]), len(b["points"]))
            for p, q, r, s in zip(a["points"], a["points"][1:], b["points"], b["points"][1:]):
                self.assertAlmostEqual(math.dist(r, s), math.dist(p, q) * 0.7, delta=0.002)

    def test_centered_layout_handles_page_edges_and_flat_strokes(self):
        for points in ([[10, 10], [287, 200]], [[20, 20], [20, 40]], [[20, 20], [40, 20]]):
            plan = studio.centered_plan(studio.to_plan({"strokes": [{"kind": "polyline", "points": points}]}))
            for x, y in plan.commands[0]["points"]:
                self.assertGreaterEqual(x, 51.55)
                self.assertLessEqual(x, 245.45)
                self.assertGreaterEqual(y, 38.5)
                self.assertLessEqual(y, 171.5)


class LayoutArtifactTests(unittest.TestCase):
    def test_saved_svg_canvas_plan_and_plotter_geometry_share_one_layout(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(studio, "resolve_axicli", side_effect=RuntimeError("offline test")):
            output = Path(folder)
            directory = output / "cccccccccccc"
            raw = geometry()
            receipt = {"latency_ms": 1234, "raw_sha256": "test-source"}
            created_at = "2026-09-09T00:00:00+00:00"
            with patch.object(studio, "OUTPUTS", output):
                record = studio.build_artifacts(raw, receipt, directory, "Test drawing", created_at=created_at)
                first_svg = (directory / "drawing.svg").read_text()
                self.assertEqual(record["layout"], studio.DRAWING_LAYOUT)
                self.assertEqual(record["created_at"], created_at)
                self.assertEqual(record["raw"], raw)
                self.assertEqual(record["metrics"]["latency_ms"], 1234)
                self.assertEqual(studio.validated_svg(directory.name), first_svg)
                self.assertEqual(json.loads((directory / "plan.json").read_text())["commands"], record["plan"]["commands"])
                self.assertEqual(first_svg, studio.plan_to_svg(studio.sanitize_plan(record["plan"], source_model=studio.MODEL)))
                studio.build_artifacts(raw, receipt, directory, "Test drawing", created_at=created_at)
                self.assertEqual((directory / "drawing.svg").read_text(), first_svg)
                record["layout"] = {**record["layout"], "scale": 0.9}
                studio.dump(directory / "record.json", record)
                with self.assertRaises(ValueError):
                    studio.validated_svg(directory.name)


class MenuTests(unittest.TestCase):
    def test_plot_menu_is_separate_from_calibration(self):
        class Menus(HTMLParser):
            def __init__(self):
                super().__init__()
                self.dialog = None
                self.elements = {}
                self.ids = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "dialog":
                    self.dialog = attrs["id"]
                if "id" in attrs:
                    self.ids.append(attrs["id"])
                    self.elements[attrs["id"]] = (tag, self.dialog)

            def handle_endtag(self, tag):
                if tag == "dialog":
                    self.dialog = None

        menus = Menus()
        menus.feed((studio.STATIC / "index.html").read_text())
        self.assertEqual(len(menus.ids), len(set(menus.ids)))
        for name in ("plot-settings", "plot-pen-raise", "plot-set-home", "plot-motion-test", "plot-telemetry"):
            self.assertEqual(menus.elements[name][1], "calibration-dialog")
        for name in ("quick-ready", "plot-start", "quick-status"):
            self.assertEqual(menus.elements[name][1], "plot-dialog")
        for name in ("plot-open", "calibration-open", "plot-home"):
            self.assertEqual(menus.elements[name], ("button", None))

    def test_spacebar_kiosk_workflow_is_present(self):
        html = (studio.STATIC / "index.html").read_text()
        script = (studio.STATIC / "app.js").read_text()
        self.assertIn('id="space-shortcut"', html)
        self.assertIn('event.code !== "Space"', script)
        self.assertIn("startSpaceWorkflow()", script)
        self.assertIn("await plotSpaceDrawing(job.record)", script)
        self.assertIn('document.querySelector("dialog[open]")', script)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name)
        cls.override = patch.object(studio, "OUTPUTS", cls.output)
        cls.override.start()
        cls.server = studio.ThreadingHTTPServer(("127.0.0.1", 0), studio.Handler)
        cls.server.studio = studio.Studio("local")
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        cls.override.stop()
        cls.temp.cleanup()

    def test_json_artifact_is_served_as_bytes(self):
        folder = self.output / "aaaaaaaaaaaa"
        folder.mkdir(exist_ok=True)
        studio.dump(folder / "metrics.json", {"test": True})
        with urllib.request.urlopen(self.url + "/artifacts/aaaaaaaaaaaa/metrics.json") as response:
            self.assertEqual(json.load(response), {"test": True})

    def test_cross_origin_generation_is_rejected(self):
        request = urllib.request.Request(self.url + "/api/draw", data=b"{}", headers={
            "Content-Type": "application/json", "Origin": "https://example.com"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)

    def test_invalid_request_does_not_start_a_model_call(self):
        request = urllib.request.Request(self.url + "/api/draw", data=b'{"prompt":""}',
                                         headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.server.studio.snapshot()["state"], "idle")

    def test_hardware_is_disabled_by_default(self):
        request = urllib.request.Request(self.url + "/api/plot", data=b"{}",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 409)
        self.assertIsNone(self.server.studio.plotter.worker)

    def test_cross_origin_hardware_requests_are_rejected(self):
        for path in ("/api/plot", "/api/plot/preflight", "/api/plot/pause"):
            request = urllib.request.Request(self.url + path, data=b"{}", headers={
                "Content-Type": "application/json", "Origin": "https://example.com"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)

    def test_changed_drawing_cannot_be_plotted(self):
        run_id = "cccccccccccc"
        folder = self.output / run_id
        folder.mkdir(exist_ok=True)
        raw = geometry()
        plan = studio.to_plan(raw)
        plan.commands[:] = studio.optimize(plan)
        studio.dump(folder / "record.json", {"raw": raw, "plan": studio.asdict(plan)})
        svg = studio.plan_to_svg(plan)
        (folder / "drawing.svg").write_text(svg)
        self.assertEqual(studio.validated_svg(run_id), svg)
        plan.commands.reverse()
        for command in plan.commands:
            command["points"].reverse()
        studio.dump(folder / "record.json", {"raw": raw, "plan": studio.asdict(plan)})
        svg = studio.plan_to_svg(plan)
        (folder / "drawing.svg").write_text(svg)
        self.assertEqual(studio.validated_svg(run_id), svg)
        (folder / "drawing.svg").write_text(svg.replace("polyline", "script"))
        with self.assertRaises(ValueError):
            studio.validated_svg(run_id)
        with self.assertRaises(ValueError):
            studio.validated_svg("../../etc/passwd")
        plan.commands[0]["points"][0][0] += 1
        studio.dump(folder / "record.json", {"raw": raw, "plan": studio.asdict(plan)})
        (folder / "drawing.svg").write_text(studio.plan_to_svg(plan))
        with self.assertRaises(ValueError):
            studio.validated_svg(run_id)

    def test_plot_receipt_can_be_read_but_arbitrary_files_cannot(self):
        folder = self.output / "dddddddddddd" / "plots"
        folder.mkdir(parents=True, exist_ok=True)
        studio.dump(folder / "eeeeeeeeeeee.json", {"physical_output_verified": False})
        with urllib.request.urlopen(self.url + "/artifacts/dddddddddddd/plots/eeeeeeeeeeee.json") as response:
            self.assertFalse(json.load(response)["physical_output_verified"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + "/artifacts/dddddddddddd/plots/../../request.json")
        self.assertEqual(caught.exception.code, 404)

    def test_guarded_plot_http_flow_with_simulated_driver(self):
        run_id = "eeeeeeeeeeee"
        folder = self.output / run_id
        folder.mkdir(exist_ok=True)
        raw = geometry()
        plan = studio.to_plan(raw)
        studio.dump(folder / "record.json", {"raw": raw, "plan": studio.asdict(plan)})
        (folder / "drawing.svg").write_text(studio.plan_to_svg(plan))
        plotter = hardware.Plotter(self.output, enabled=True)
        driver = Mock()
        driver.errors.code = driver.plot_status.stopped = 0
        driver.warnings.return_text_list.return_value = []
        driver.time_estimate = 1.0
        driver.distance_pendown = 0.1
        driver.params.native_res_factor = 1016.0
        enabled = {"motor_steps": [0, 0], "motor_microsteps": [16, 16], "pen_reported_up": True,
                   "servo_power_on": True, "queue_idle": True, "motors_enabled": True}
        released = {**enabled, "motor_microsteps": [0, 0], "motors_enabled": False}

        def execute():
            if driver.options.mode == "manual" and driver.options.manual_cmd == "enable_xy":
                controller.return_value = enabled
        driver.plot_run.side_effect = execute

        def post(path, value):
            request = urllib.request.Request(self.url + path, data=json.dumps(value).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                return json.load(response)

        with patch.object(self.server.studio, "plotter", plotter), \
                patch.object(hardware, "preview_svg", return_value={"estimated_seconds": 1}), \
                patch.object(hardware, "checked_connection", side_effect=lambda _: nullcontext((object(), {"firmware": "HTTP fixture"}))), \
                patch.object(hardware, "controller_state", return_value=released) as controller, \
                patch.object(hardware, "wait_idle"), \
                patch.object(hardware, "make_driver", return_value=driver):
            try:
                checked = post("/api/plot/preflight", {"run_id": run_id, "settings": {"port": "/dev/http-test"}})
                self.assertIsNone(plotter.worker)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    post("/api/plot", {"run_id": run_id, "token": checked["token"], "confirm_ready": False})
                self.assertEqual(caught.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    post("/api/plot", {"run_id": run_id, "token": checked["token"], "confirm_ready": True})
                self.assertEqual(caught.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    post("/api/plot", {"run_id": run_id, "token": checked["token"], "confirm_ready": True, "confirm_home": True})
                self.assertEqual(caught.exception.code, 400)
                post("/api/plot/test", {"run_id": run_id, "token": checked["token"], "confirm_ready": True,
                                        "confirm_home": True, "action": "set_home"})
                plotter.worker.join(3)
                self.assertEqual(plotter.snapshot()["job"]["state"], "complete")
                checked = post("/api/plot/preflight", {"run_id": run_id, "settings": {"port": "/dev/http-test"}})
                self.assertTrue(checked["home_ready"])
                job = post("/api/plot", {"run_id": run_id, "token": checked["token"], "confirm_ready": True, "confirm_home": True})
                plotter.worker.join(3)
                self.assertEqual(plotter.snapshot()["job"]["state"], "complete")
                with urllib.request.urlopen(self.url + f'/artifacts/{run_id}/plots/{job["id"]}.json') as response:
                    self.assertFalse(json.load(response)["physical_output_verified"])
                checked = post("/api/plot/preflight", {"run_id": run_id, "settings": {"port": "/dev/http-test"}})
                request = {"run_id": run_id, "token": checked["token"], "confirm_ready": True}
                for action in ("home", "motion_test"):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        post("/api/plot/test", {**request, "action": action})
                    self.assertEqual(caught.exception.code, 400)
                job = post("/api/plot/test", {**request, "action": "pen_up"})
                plotter.worker.join(3)
                self.assertEqual(plotter.snapshot()["job"]["state"], "complete")
                self.assertEqual(job["action"], "pen_up")
                with patch.object(hardware, "checked_connection", side_effect=hardware.PlotterCommunicationError("USB timeout after QC")):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        post("/api/plot/preflight", {"run_id": run_id, "settings": {"port": "/dev/http-test"}})
                    self.assertEqual(caught.exception.code, 409)
            finally:
                plotter.close()

    def test_standalone_home_http_flow_needs_no_saved_artwork(self):
        from test_astra_plotter import CONTROLLER, Driver
        plotter = hardware.Plotter(self.output, enabled=True)
        settings = {"port": "/dev/http-test", "model": 1}
        device = {"firmware": "HTTP fixture"}
        plotter.remember_home(settings, device, CONTROLLER)
        driver = Driver()

        def make_driver(svg, options, **kwargs):
            from types import SimpleNamespace
            driver.options = SimpleNamespace(**options)
            return driver

        def post(path, value):
            request = urllib.request.Request(self.url + path, data=json.dumps(value).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                return json.load(response)

        with patch.object(self.server.studio, "plotter", plotter), \
                patch.object(studio, "validated_svg", side_effect=AssertionError("Home must not read artwork")), \
                patch.object(hardware, "preview_svg", return_value={"estimated_seconds": 1}), \
                patch.object(hardware, "checked_connection", side_effect=lambda _: nullcontext((object(), device))), \
                patch.object(hardware, "controller_state", return_value=CONTROLLER), \
                patch.object(hardware, "wait_idle"), \
                patch.object(hardware, "make_driver", side_effect=make_driver):
            try:
                checked = post("/api/plot/preflight", {"settings": settings})
                self.assertEqual(checked["run_id"], hardware.DEVICE_RUN_ID)
                request = {"run_id": hardware.DEVICE_RUN_ID, "token": checked["token"],
                           "confirm_ready": True, "confirm_home": True}
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    post("/api/plot", request)
                self.assertEqual(caught.exception.code, 400)
                self.assertIsNone(plotter.worker)
                job = post("/api/plot/test", {**request, "action": "home"})
                plotter.worker.join(3)
                self.assertEqual(plotter.snapshot()["job"]["state"], "complete")
                self.assertEqual([c["manual_cmd"] for c in driver.calls], ["raise_pen", "walk_home"])
                with urllib.request.urlopen(self.url + f'/artifacts/plotter/plots/{job["id"]}.json') as response:
                    self.assertEqual(json.load(response)["action"], "home")
                for path in ("/artifacts/plotter/raw_response.json", "/artifacts/plotter/plots/../../request.json"):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(self.url + path)
                    self.assertEqual(caught.exception.code, 404)
            finally:
                plotter.close()

    def test_hardware_button_icons_are_served_locally(self):
        for name in ("printer", "house", "sliders-horizontal"):
            with urllib.request.urlopen(self.url + f"/icons/{name}.svg") as response:
                self.assertIn("image/svg+xml", response.headers["Content-Type"])
                self.assertIn(b"<svg", response.read())

    def test_bad_photo_does_not_start_a_model_call(self):
        request = urllib.request.Request(self.url + "/api/draw",
            data=b'{"prompt":"Draw the subject","image_data_url":"not a photo"}',
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.server.studio.snapshot()["state"], "idle")

    def test_source_photo_is_local_and_microphone_is_disabled(self):
        folder = self.output / "bbbbbbbbbbbb"
        folder.mkdir(exist_ok=True)
        photo = studio.decode_photo(jpeg_data_url())
        (folder / "source.jpg").write_bytes(photo.data)
        with urllib.request.urlopen(self.url + "/artifacts/bbbbbbbbbbbb/source.jpg") as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertEqual(response.read(), photo.data)
            self.assertEqual(response.headers["Permissions-Policy"], "camera=(self), microphone=()")

    def test_busy_run_rejects_duplicate_generation(self):
        request = urllib.request.Request(self.url + "/api/draw", data=b'{"prompt":"A flower"}',
                                         headers={"Content-Type": "application/json"})
        with patch.object(self.server.studio, "job", {"state": "running"}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
