"use strict";
const $ = (id) => document.getElementById(id);
const canvas = $("drawing"),
  ctx = canvas.getContext("2d");
const presets = {
  koi: "A graceful koi curling around a crescent moon. Flowing fins, three ripples, a few tiny stars. Elegant Japanese ink contours, spacious and balanced.",
  tokyo:
    "Tokyo at dusk seen across a river. A recognizable Tokyo Tower, layered low rooftops, Mount Fuji in the distance, a crescent moon, elegant river reflections. Architectural pen sketch, no text.",
  iris: "A mechanical iris blooming from a slender botanical stem. Overlapping curved aperture blades, delicate petal veins, two graceful leaves. A precise but organic engineering-meets-botany pen drawing. No labels.",
};
let records = [],
  selected = null,
  segments = [],
  total = 0,
  cursor = 0;
let playing = false,
  frame = null,
  lastTime = 0,
  activeRun = null,
  busy = false;
let spaceWorkflow = false,
  spaceRunId = null,
  spacePlotJobId = null;
let sourceMode = "prompt",
  cameraStream = null,
  cameraOpening = false,
  cameraEpoch = 0,
  capturedPhoto = null,
  cameraError = "";
const promptDrafts = {
  prompt: $("prompt").value,
  webcam:
    "Turn this photo into a recognizable, elegant pen drawing. Preserve the main subject's outline, proportions, and distinctive details. Simplify background clutter. No text.",
};

function updateCameraControls() {
  const video = $("camera-video");
  $("camera-panel").hidden = sourceMode !== "webcam";
  video.hidden = !cameraStream;
  $("camera-photo").hidden = !capturedPhoto || !!cameraStream;
  $("camera-placeholder").hidden = !!capturedPhoto || !!cameraStream;
  $("camera-toggle").hidden =
    !!capturedPhoto && !cameraStream && !cameraOpening;
  $("camera-toggle").textContent = cameraOpening
    ? "Cancel camera"
    : cameraStream
      ? "Stop camera"
      : "Start camera";
  $("camera-toggle").disabled = busy && !cameraStream && !cameraOpening;
  $("camera-capture").hidden = !cameraStream;
  $("camera-capture").disabled =
    busy || video.readyState < 2 || !video.videoWidth;
  $("camera-retake").hidden = !capturedPhoto || !!cameraStream || cameraOpening;
  $("camera-retake").disabled = busy;
  $("camera-status").textContent =
    cameraError ||
    (cameraOpening
      ? "Starting camera..."
      : cameraStream
        ? "Camera on."
        : capturedPhoto
          ? "Photo ready. Camera off."
          : "Camera off.");
  $("draw").disabled =
    busy ||
    (sourceMode === "webcam" &&
      (!capturedPhoto || cameraOpening || !!cameraStream));
  $("refine").disabled = sourceMode === "webcam" || !selected;
}

function stopCamera() {
  cameraEpoch += 1;
  cameraOpening = false;
  const stream = cameraStream;
  cameraStream = null;
  stream?.getTracks().forEach((track) => track.stop());
  $("camera-video").srcObject = null;
  updateCameraControls();
}

async function startCamera() {
  if (busy || cameraOpening || cameraStream) return;
  if (!navigator.mediaDevices?.getUserMedia) {
    cameraError =
      "Camera unavailable. Open this studio on localhost in a camera-enabled browser.";
    updateCameraControls();
    return;
  }
  const epoch = ++cameraEpoch;
  cameraOpening = true;
  cameraError = "";
  updateCameraControls();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: "user",
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });
    // A permission dialog can outlive a mode change or Cancel click.
    if (epoch !== cameraEpoch || sourceMode !== "webcam") {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    cameraStream = stream;
    cameraOpening = false;
    capturedPhoto = null;
    $("camera-photo").removeAttribute("src");
    stream.getVideoTracks().forEach((track) =>
      track.addEventListener("ended", () => {
        if (cameraStream !== stream) return;
        cameraError = "Camera disconnected. Start it again to capture a photo.";
        stopCamera();
      }),
    );
    $("camera-video").srcObject = stream;
    updateCameraControls();
    await $("camera-video").play();
    if (epoch === cameraEpoch) updateCameraControls();
  } catch (error) {
    if (epoch !== cameraEpoch) return;
    const messages = {
      NotAllowedError:
        "Camera access was denied. Allow camera access for localhost and try again.",
      NotFoundError: "No webcam was found.",
      NotReadableError: "The webcam is unavailable or in use by another app.",
    };
    cameraError =
      messages[error.name] || "Could not start the webcam. Please try again.";
    stopCamera();
  }
}

function capturePhoto() {
  const video = $("camera-video");
  if (busy || !cameraStream || video.readyState < 2 || !video.videoWidth)
    return null;
  try {
    const image = document.createElement("canvas");
    const scale = Math.min(
      1,
      960 / Math.max(video.videoWidth, video.videoHeight),
    );
    image.width = Math.max(1, Math.round(video.videoWidth * scale));
    image.height = Math.max(1, Math.round(video.videoHeight * scale));
    image.getContext("2d").drawImage(video, 0, 0, image.width, image.height);
    const data = image.toDataURL("image/jpeg", 0.85);
    if (!data.startsWith("data:image/jpeg;base64,") || data.length > 2_000_000)
      throw new Error(
        "Could not capture a small JPEG. Please take the photo again.",
      );
    capturedPhoto = data;
    $("camera-photo").src = data;
    cameraError = "";
    stopCamera();
    return capturedPhoto;
  } catch (error) {
    cameraError = error.message;
    updateCameraControls();
    return null;
  }
}

async function waitForCameraFrame() {
  const video = $("camera-video");
  if (video.readyState >= 2 && video.videoWidth) return;
  await new Promise((resolve, reject) => {
    const ready = () => {
      if (video.readyState < 2 || !video.videoWidth) return;
      cleanup();
      resolve();
    };
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("The webcam did not produce a frame. Press Space to try again."));
    }, 8000);
    const cleanup = () => {
      clearTimeout(timeout);
      video.removeEventListener("loadeddata", ready);
      video.removeEventListener("canplay", ready);
    };
    video.addEventListener("loadeddata", ready);
    video.addEventListener("canplay", ready);
  });
}

function selectWebcamSource() {
  const webcam = document.querySelector('input[name="source"][value="webcam"]');
  if (webcam.checked) return;
  webcam.checked = true;
  webcam.dispatchEvent(new Event("change", { bubbles: true }));
}

document.querySelectorAll("input[name=source]").forEach((input) =>
  input.addEventListener("change", () => {
    promptDrafts[sourceMode] = $("prompt").value;
    sourceMode = input.value;
    $("prompt").value = promptDrafts[sourceMode];
    $("prompt").rows = sourceMode === "webcam" ? 3 : 5;
    $("prompt-label").textContent =
      sourceMode === "webcam" ? "Photo direction" : "What goes on the page?";
    $("preset-row").hidden = sourceMode === "webcam";
    $("refine-row").hidden = sourceMode === "webcam";
    $("draw-label").textContent =
      sourceMode === "webcam" ? "Draw photo" : "Draw with Astra";
    document.body.classList.toggle("photo-mode", sourceMode === "webcam");
    cameraError = "";
    if (sourceMode !== "webcam") stopCamera();
    updateCameraControls();
  }),
);
$("camera-toggle").addEventListener("click", () => {
  cameraError = "";
  if (cameraStream || cameraOpening) stopCamera();
  else startCamera();
});
$("camera-capture").addEventListener("click", capturePhoto);
$("camera-retake").addEventListener("click", startCamera);
$("camera-video").addEventListener("loadeddata", updateCameraControls);
window.addEventListener("pagehide", stopCamera);

let plotState = { enabled: false, job: { state: "idle" } },
  plotRun = null,
  preflight = null,
  plotEpoch = 0,
  plotRequestBusy = false,
  plotSubmitting = false,
  plotMode = "plot",
  activePlotDialog = null,
  preflightTimer = null;
const plotActionNames = {
  plot: "Drawing",
  home: "Returning Home",
  align: "Pen lift and XY release",
  set_home: "Setting Home",
  pen_up: "Pen lift",
  pen_cycle: "Pen down / up",
  motion_test: "5 mm pen-up square",
};
const plotPhaseNames = {
  raising_pen: "Raising pen",
  releasing_xy: "Releasing XY motors",
  returning_home: "Returning to Home",
  setting_home: "Setting Home",
};

function updatePlotter(value = plotState) {
  plotState = value;
  const job = value.job;
  const active = ["checking", "running", "pausing"].includes(job.state);
  const unavailable = !value.enabled || active || busy || plotRequestBusy || plotSubmitting;
  $("plot-open").disabled = unavailable || !selected;
  $("calibration-open").disabled = unavailable;
  $("plot-home").disabled = unavailable;
  $("plot-pause").hidden = !["running", "pausing"].includes(job.state);
  $("plot-pause").disabled = job.state === "pausing";
  $("plot-mode").textContent = value.enabled
    ? "Physical plotting enabled"
    : "Simulation only";
  const messages = {
    idle: value.enabled
      ? "Ready for device check."
      : "Disabled. Start server with --enable-plotter.",
    checking: "Checking geometry and USB. No movement.",
    running: `${plotPhaseNames[job.phase] || plotActionNames[job.action] || "Drawing"} in progress. Keep the workspace clear.`,
    pausing: "Pause requested. Wait for the machine to stop.",
    paused: job.message,
    complete: job.message,
    error: job.error,
  };
  $("plot-status").textContent =
    messages[job.state] || "Plotter state unavailable.";
  $("plot-serial").hidden = !active || !job.last_command;
  $("plot-serial").textContent = job.last_command
    ? `USB sent: ${job.last_command}\nLast reply: ${job.last_response || "Waiting..."}`
    : "";
  $("plot-receipt").hidden = active || !job.finished_at;
  if (job.finished_at)
    $("plot-receipt").href = `/artifacts/${job.run_id}/plots/${job.id}.json`;
}

function invalidatePreflight() {
  clearTimeout(preflightTimer);
  preflight = null;
  $("quick-ready").checked = false;
  $("quick-ready").disabled = true;
  $("plot-ready").checked = false;
  $("plot-ready").disabled = true;
  $("plot-motion-ready").checked = false;
  $("plot-motion-ready").disabled = true;
  $("plot-home-ready").checked = false;
  $("plot-home-ready").disabled = true;
  $("plot-telemetry").hidden = true;
  updatePlotActions();
}

function updatePlotActions() {
  const waiting = plotRequestBusy || plotSubmitting;
  const checked = plotMode === "calibration" && !!preflight && !waiting;
  const movementReady = checked && $("plot-ready").checked;
  for (const id of ["plot-align", "plot-pen-raise"])
    $(id).disabled = !checked;
  $("plot-pen-cycle").disabled = !movementReady;
  $("plot-home-ready").disabled = !checked;
  $("quick-ready").disabled = waiting || !preflight?.home_ready;
  $("plot-start").disabled = waiting || !preflight?.home_ready || !$("quick-ready").checked;
  $("plot-set-home").disabled = !checked || !$("plot-home-ready").checked;
  $("plot-motion-ready").disabled = !movementReady;
  $("plot-motion-test").disabled =
    !movementReady || !preflight.home_ready || !$("plot-home-ready").checked || !$("plot-motion-ready").checked;
  for (const id of ["plot-check", "quick-check", "quick-calibration", "plot-settings"])
    $(id).disabled = waiting;
  for (const id of ["plot-cancel", "quick-cancel"])
    $(id).disabled = plotSubmitting;
}

function plotStatus(message) {
  $(plotMode === "calibration" ? "plot-check-status" : "quick-status").textContent = message;
}

function plotSettings() {
  return {
    port: $("plot-port").value,
    model: Number($("plot-model").value),
    speed_pendown: Number($("plot-speed").value),
    speed_penup: Number($("plot-travel-speed").value),
    accel: Number($("plot-accel").value),
    pen_pos_up: Number($("plot-pen-up").value),
    pen_pos_down: Number($("plot-pen-down").value),
  };
}

async function loadPlotDevices(epoch) {
  const device = await api("/api/plotter");
  if (epoch !== plotEpoch) return null;
  const previous = $("plot-port").value;
  $("plot-port").replaceChildren(new Option("Select AxiDraw", ""));
  for (const item of device.devices)
    $("plot-port").add(new Option(`${item.label} / ${item.port}`, item.port));
  $("plot-port").value = device.devices.some((d) => d.port === previous)
    ? previous
    : device.devices.length === 1
      ? device.devices[0].port
      : "";
  return device;
}

async function openPlotControls(mode) {
  if (plotRequestBusy || plotSubmitting || (mode === "plot" && !selected)) return;
  plotMode = mode;
  plotRun = mode === "plot" ? selected : null;
  const dialog = mode === "calibration" ? "calibration-dialog" : "plot-dialog";
  const previous = activePlotDialog;
  activePlotDialog = dialog;
  if (previous && previous !== dialog && $(previous).open) $(previous).close();
  plotEpoch += 1;
  invalidatePreflight();
  if (mode !== "calibration") {
    $("plot-heading").textContent = mode === "home" ? "Send plotter Home" : "Plot on AxiDraw";
    $("plot-drawing-name").textContent = plotRun ? `${plotRun.title} / A4 landscape` : "Saved Home (0, 0) / pen raised";
    $("quick-device").textContent = "Checking device...";
    $("quick-action").textContent = mode === "home" ? "Send Home" : "Home & start plot";
    $("quick-icon").src = mode === "home" ? "/icons/house.svg" : "/icons/printer.svg";
    $("quick-confirm-label").textContent = mode === "home"
      ? "Power and pen checked, other plotter apps closed, and workspace clear. The head has not been moved by hand or stalled since Home was set."
      : "Power and pen checked, other plotter apps closed, A4 aligned, and workspace clear. The head has not been moved by hand or stalled since Home was set.";
    $("quick-calibration").hidden = true;
  }
  $(dialog).showModal();
  await checkPlotter();
}
$("plot-open").addEventListener("click", () => openPlotControls("plot"));
$("calibration-open").addEventListener("click", () => openPlotControls("calibration"));
$("plot-home").addEventListener("click", () => openPlotControls("home"));
$("quick-calibration").addEventListener("click", () => openPlotControls("calibration"));
$("plot-settings").addEventListener("input", () => {
  invalidatePreflight();
  $("plot-check-status").textContent =
    "Settings changed. Check the plotter again.";
});
async function checkPlotter() {
  if (plotRequestBusy || plotSubmitting) return;
  const epoch = plotEpoch;
  plotRequestBusy = true;
  invalidatePreflight();
  updatePlotter();
  plotStatus("Checking USB, power, and Home. No movement.");
  try {
    const device = await loadPlotDevices(epoch);
    if (!device || epoch !== plotEpoch) return;
    if (!device.enabled || !device.available || !device.devices.length)
      throw new Error(device.message || "Physical plotting is unavailable.");
    $("plot-settings").disabled = false;
    const valid = plotMode === "calibration"
      ? $("calibration-form").reportValidity()
      : $("calibration-form").checkValidity();
    if (!valid) throw new Error("Review the device and settings in Calibration.");
    const settings = plotSettings();
    $("plot-settings").disabled = true;
    $("quick-device").textContent = `${$("plot-port").selectedOptions[0].textContent} / ${$("plot-model").selectedOptions[0].textContent}`;
    const checked = await api("/api/plot/preflight", {
      run_id: plotRun?.id || "plotter",
      settings,
    });
    if (epoch !== plotEpoch) return;
    preflight = checked;
    $("plot-ready").disabled = false;
    if (plotMode === "calibration") {
      plotStatus(checked.home_ready ? "Home reference checked. Device ready."
        : "Home not set. If the carriage is already fully left and back, confirm its position below and set it directly. Otherwise release XY first.");
    } else {
      plotStatus(checked.home_ready
        ? (plotMode === "home" ? "Ready to return Home." : `Ready. Estimated drawing time: ${checked.preview.estimated_seconds} s, plus return Home.`)
        : "Home needs to be set in Calibration first.");
      $("quick-calibration").hidden = checked.home_ready;
    }
    preflightTimer = setTimeout(() => {
      if (epoch !== plotEpoch) return;
      invalidatePreflight();
      plotStatus("Check expired. Check again before moving the plotter.");
    }, 115000);
    $("plot-home-label").textContent = checked.home_ready
      ? "A4 paper is aligned to the saved Home. The head has not been moved by hand or stalled."
      : "The carriage is physically at Home (fully left and back), and A4 landscape's upper-left corner is under the tip. Set Home will raise the pen and release XY if needed.";
    const controller = checked.device.controller;
    $("plot-telemetry").textContent = [
      checked.device.firmware,
      `Power ADC: ${checked.device.power_adc_counts} (presence only, not adapter verification)`,
      `Pen reported: ${controller.pen_reported_up ? "up" : "down"} / servo power: ${controller.servo_power_on ? "on" : "off"}`,
      `Motor counters: ${controller.motor_steps.join(", ")} / XY: ${controller.motors_enabled ? "energized" : "released"} / queue: ${controller.queue_idle ? "idle" : "busy"}`,
      "Controller readings do not measure physical pen height or position.",
    ].join("\n");
    $("plot-telemetry").hidden = false;
  } catch (error) {
    if (epoch === plotEpoch) {
      plotStatus(error.message);
      $("quick-calibration").hidden = false;
      if (!$("plot-port").value) $("quick-device").textContent = "No device selected";
    }
  } finally {
    plotRequestBusy = false;
    if (epoch === plotEpoch) {
      updatePlotActions();
    }
    updatePlotter();
  }
}
$("plot-check").addEventListener("click", checkPlotter);
$("quick-check").addEventListener("click", checkPlotter);
$("plot-ready").addEventListener("change", updatePlotActions);
$("plot-motion-ready").addEventListener("change", updatePlotActions);
$("plot-home-ready").addEventListener("change", updatePlotActions);
$("quick-ready").addEventListener("change", updatePlotActions);
for (const id of ["paper-setup", "plot-setup"])
  $(id).addEventListener("click", () => $("setup-dialog").showModal());
$("plot-cancel").addEventListener("click", () => $("calibration-dialog").close());
$("quick-cancel").addEventListener("click", () => $("plot-dialog").close());
for (const id of ["plot-dialog", "calibration-dialog"]) {
  $(id).addEventListener("cancel", (event) => {
    if (plotSubmitting) event.preventDefault();
  });
  $(id).addEventListener("close", () => {
    if ($(id).open || activePlotDialog !== id) return;
    activePlotDialog = null;
    plotEpoch += 1;
    invalidatePreflight();
  });
}
async function submitPlotAction(action) {
  const calibration = plotMode === "calibration";
  const directCalibration = calibration && ["align", "pen_up", "set_home"].includes(action);
  const confirmed = directCalibration || $(calibration ? "plot-ready" : "quick-ready").checked;
  const homed = calibration ? $("plot-home-ready").checked : confirmed;
  if (!preflight || !confirmed || plotSubmitting || plotRequestBusy) return;
  if (action === "motion_test" && !$("plot-motion-ready").checked) return;
  if (["plot", "home", "motion_test", "set_home"].includes(action) && !homed)
    return;
  if (["plot", "home", "motion_test"].includes(action) && !preflight.home_ready) return;
  plotSubmitting = true;
  const request = {
    run_id: plotRun?.id || "plotter",
    token: preflight.token,
    confirm_ready: true,
    action,
    confirm_pen_clear: $("plot-motion-ready").checked,
    confirm_home: homed,
  };
  invalidatePreflight();
  updatePlotter();
  try {
    const job = await api(
      action === "plot" ? "/api/plot" : "/api/plot/test",
      request,
    );
    updatePlotter({ ...plotState, job });
    $(activePlotDialog).close();
  } catch (error) {
    plotStatus(`${error.message} Check plot status before retrying.`);
  } finally {
    plotSubmitting = false;
    updatePlotActions();
    updatePlotter();
  }
}
$("plot-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitPlotAction(plotMode);
});
$("calibration-form").addEventListener("submit", (event) => event.preventDefault());
$("plot-pen-raise").addEventListener("click", () => submitPlotAction("pen_up"));
$("plot-align").addEventListener("click", () => submitPlotAction("align"));
$("plot-set-home").addEventListener("click", () => submitPlotAction("set_home"));
$("plot-pen-cycle").addEventListener("click", () =>
  submitPlotAction("pen_cycle"),
);
$("plot-motion-test").addEventListener("click", () =>
  submitPlotAction("motion_test"),
);
$("plot-pause").addEventListener("click", async () => {
  $("plot-pause").disabled = true;
  try {
    const job = await api("/api/plot/pause", { job_id: plotState.job.id });
    updatePlotter({ ...plotState, job });
  } catch (error) {
    $("plot-status").textContent =
      `Pause not confirmed: ${error.message}. Use the physical pause button.`;
    $("plot-pause").disabled = false;
  }
});

async function api(path, data) {
  const response = await fetch(
    path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        },
  );
  const body = await response.json();
  if (!response.ok)
    throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function artifact(record, name) {
  return `/artifacts/${record.id}/${name}`;
}
function setPlaying(value) {
  playing = value;
  $("play-icon").src = `/icons/${value ? "pause" : "play"}.svg`;
  $("play").setAttribute(
    "aria-label",
    value ? "Pause pen simulation" : "Play pen simulation",
  );
  if (frame) cancelAnimationFrame(frame);
  frame = null;
  if (value) {
    lastTime = performance.now();
    frame = requestAnimationFrame(tick);
  }
}
function tick(now) {
  cursor = Math.min(
    total,
    cursor +
      (Math.min(now - lastTime, 100) / 1000) * 28 * Number($("speed").value),
  );
  lastTime = now;
  paint();
  if (cursor >= total) setPlaying(false);
  else frame = requestAnimationFrame(tick);
}
function prepare(record) {
  segments = [];
  total = 0;
  let point = [0, 0];
  function add(to, ink, stroke) {
    const length = Math.hypot(to[0] - point[0], to[1] - point[1]);
    if (length > 0) {
      segments.push({ a: point, b: to, length, start: total, ink, stroke });
      total += length;
    }
    point = to;
  }
  record.plan.commands.forEach((command, i) => {
    add(command.points[0], false, i);
    command.points.slice(1).forEach((p) => add(p, true, i));
  });
  add([0, 0], false, record.plan.commands.length);
}
function paint() {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth,
    height = canvas.clientHeight;
  if (!width || !height) return;
  if (
    canvas.width !== Math.round(width * ratio) ||
    canvas.height !== Math.round(height * ratio)
  ) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  ctx.setTransform(canvas.width / 297, 0, 0, canvas.height / 210, 0, 0);
  ctx.clearRect(0, 0, 297, 210);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  let head = [0, 0],
    stroke = 0,
    penDown = false;
  for (const segment of segments) {
    if (segment.start >= cursor) break;
    const t = Math.min(1, (cursor - segment.start) / segment.length);
    const end = segment.a.map((a, i) => a + (segment.b[i] - a) * t);
    if (segment.ink || $("travel").checked) {
      ctx.strokeStyle = segment.ink ? "#202728" : "#c5d4cf";
      ctx.lineWidth = segment.ink ? 0.35 : 0.22;
      ctx.setLineDash(segment.ink ? [] : [1, 1]);
      ctx.beginPath();
      ctx.moveTo(...segment.a);
      ctx.lineTo(...end);
      ctx.stroke();
    }
    head = end;
    stroke = segment.stroke;
    penDown = segment.ink;
  }
  ctx.setLineDash([]);
  if (cursor > 0 && cursor < total) {
    ctx.strokeStyle = penDown ? "#ec4c36" : "#167668";
    ctx.lineWidth = 0.4;
    ctx.beginPath();
    ctx.arc(...head, 2.2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.arc(...head, 0.7, 0, Math.PI * 2);
    ctx.fill();
  }
  $("scrub").value = total ? Math.round((cursor / total) * 1000) : 0;
  const count = selected?.plan.commands.length || 0;
  const completed = cursor >= total ? count : Math.min(stroke, count);
  $("progress-label").textContent = `${completed} / ${count} STROKES`;
}
function select(record, animate = false) {
  setPlaying(false);
  selected = record;
  prepare(record);
  cursor =
    animate && !matchMedia("(prefers-reduced-motion: reduce)").matches
      ? 0
      : total;
  $("empty-state").hidden = true;
  $("drawing-title").textContent = record.title.replaceAll("_", " ");
  $("description").textContent = record.description;
  $("run-label").textContent =
    "ASTRA / SAVED RUN " + record.id.slice(0, 6).toUpperCase();
  const m = record.metrics;
  $("latency").textContent = `${(m.latency_ms / 1000).toFixed(1)} s`;
  $("strokes").textContent = m.command_count;
  $("distance").textContent =
    `${(m.estimated_pen_distance_mm / 1000).toFixed(2)} m`;
  $("plot-time").textContent = (
    m.plot_preview?.estimated_print_time || "Unavailable"
  )
    .replace(/Seconds?/g, "s")
    .replace(/Minutes?/g, "m")
    .replace(/Hours?/g, "h");
  $("download").href = artifact(record, "drawing.svg");
  $("download").classList.remove("disabled");
  $("download").removeAttribute("aria-disabled");
  $("raw-link").href = artifact(record, "raw_response.json");
  $("metrics-link").href = artifact(record, "metrics.json");
  $("preview-link").href = artifact(record, "preview.svg");
  $("preview-link").hidden = !m.preview_generated;
  $("source-photo-link").href = artifact(record, "source.jpg");
  $("source-photo-link").hidden = !m.source_image;
  $("evidence").hidden = false;
  $("warning").textContent = (m.notes || []).join(" ");
  $("warning").hidden = !m.notes?.length;
  for (const id of ["play", "replay", "scrub"]) $(id).disabled = false;
  updateCameraControls();
  updatePlotter();
  paint();
  renderGallery();
  if (cursor < total) setPlaying(true);
}
function renderGallery() {
  $("gallery").replaceChildren();
  $("drawing-count").textContent =
    `${records.length} DRAWING${records.length === 1 ? "" : "S"}`;
  for (const record of records.slice(0, 9)) {
    const button = document.createElement("button");
    button.className =
      "thumbnail" + (selected?.id === record.id ? " active" : "");
    button.type = "button";
    button.title = record.prompt;
    button.setAttribute("aria-label", `Open ${record.title}`);
    const image = document.createElement("img");
    image.src = artifact(record, "drawing.svg");
    image.alt = record.title;
    const text = document.createElement("span");
    text.textContent = record.title.replaceAll("_", " ");
    button.append(image, text);
    button.addEventListener("click", () => select(record));
    $("gallery").append(button);
  }
}
function setBusy(value) {
  busy = value;
  updateCameraControls();
  updatePlotter();
  document.body.classList.toggle("generating", value);
}
async function requestDrawing() {
  if ($("draw").disabled) throw new Error("The drawing request is not ready.");
  setBusy(true);
  $("run-status").classList.remove("error");
  $("run-status").textContent = "Contacting GPT-6 Astra...";
  try {
    const job = await api("/api/draw", {
      prompt: $("prompt").value,
      detail: document.querySelector("input[name=detail]:checked").value,
      reference_id:
        sourceMode === "prompt" && $("refine").checked ? selected?.id : null,
      image_data_url: sourceMode === "webcam" ? capturedPhoto : undefined,
    });
    activeRun = job.id;
    return job;
  } catch (error) {
    $("run-status").textContent = error.message;
    $("run-status").classList.add("error");
    setBusy(false);
    throw error;
  }
}
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await requestDrawing();
  } catch (_) {
    // requestDrawing already reports the error in the interface.
  }
});

async function plotSpaceDrawing(record) {
  $("run-status").textContent = "Drawing ready. Checking AxiDraw and saved Home...";
  const device = await api("/api/plotter");
  if (!device.enabled || !device.available || !device.devices.length)
    throw new Error(device.message || "No AxiDraw is available.");
  const currentPort = $("plot-port").value;
  const chosen = device.devices.find((item) => item.port === currentPort)
    || (device.devices.length === 1 ? device.devices[0] : null);
  if (!chosen)
    throw new Error("More than one AxiDraw is connected. Select one in Calibration once.");
  const checked = await api("/api/plot/preflight", {
    run_id: record.id,
    settings: { ...plotSettings(), port: chosen.port },
  });
  if (!checked.home_ready)
    throw new Error("Home is not set. Set it once in Calibration, then press Space again.");
  const job = await api("/api/plot", {
    run_id: record.id,
    token: checked.token,
    confirm_ready: true,
    confirm_home: true,
  });
  spacePlotJobId = job.id;
  updatePlotter({ ...plotState, job });
  $("run-status").textContent = "Photo converted. Plotting on AxiDraw...";
}

function finishSpaceWorkflow(message, error = false) {
  spaceWorkflow = false;
  spaceRunId = null;
  spacePlotJobId = null;
  document.body.classList.remove("space-running");
  $("run-status").textContent = message;
  $("run-status").classList.toggle("error", error);
}

async function startSpaceWorkflow() {
  const plotActive = ["checking", "running", "pausing"].includes(plotState.job.state);
  if (spaceWorkflow || busy || activeRun || plotActive) return;
  spaceWorkflow = true;
  document.body.classList.add("space-running");
  $("run-status").classList.remove("error");
  try {
    selectWebcamSource();
    $("run-status").textContent = "Spacebar: starting webcam...";
    if (!cameraStream) await startCamera();
    if (!cameraStream)
      throw new Error(cameraError || "The webcam could not be started.");
    await waitForCameraFrame();
    if (!capturePhoto())
      throw new Error(cameraError || "The webcam frame could not be captured.");
    $("run-status").textContent = "Photo captured. Contacting GPT-6 Astra...";
    const job = await requestDrawing();
    spaceRunId = job.id;
  } catch (error) {
    finishSpaceWorkflow(error.message, true);
  }
}

window.addEventListener("keydown", (event) => {
  if (event.code !== "Space" || event.repeat || event.altKey || event.ctrlKey || event.metaKey)
    return;
  const target = event.target;
  const editing = target instanceof HTMLElement &&
    (target.isContentEditable || !!target.closest("input, textarea, select, button"));
  if (editing || document.querySelector("dialog[open]")) return;
  event.preventDefault();
  startSpaceWorkflow();
});
$("preset").addEventListener("change", () => {
  if (presets[$("preset").value]) {
    $("prompt").value = presets[$("preset").value];
    $("refine").checked = false;
  }
});
$("prompt").addEventListener("input", () => {
  $("preset").value = "custom";
});
$("replay").addEventListener("click", () => {
  cursor = 0;
  paint();
  setPlaying(true);
});
$("play").addEventListener("click", () => {
  if (cursor >= total) cursor = 0;
  setPlaying(!playing);
});
$("travel").addEventListener("change", paint);
$("scrub").addEventListener("input", () => {
  setPlaying(false);
  cursor = (total * Number($("scrub").value)) / 1000;
  paint();
});
new ResizeObserver(paint).observe(canvas);
async function refresh() {
  try {
    const status = await api("/api/status");
    updatePlotter(status.plotter);
    const plotJob = status.plotter.job;
    if (spacePlotJobId && plotJob.id === spacePlotJobId &&
        !["checking", "running", "pausing"].includes(plotJob.state)) {
      if (plotJob.state === "complete")
        finishSpaceWorkflow("Spacebar drawing complete. Inspect the paper.");
      else
        finishSpaceWorkflow(
          plotJob.error || plotJob.message || "AxiDraw did not complete the drawing.",
          true,
        );
    }
    $("connection").textContent =
      `GPT-6 Astra / ${status.backend === "local" ? "local session" : "API"}`;
    const job = status.job;
    if (job.state === "running") {
      activeRun = job.id;
      setBusy(true);
      const elapsed = Math.max(0, Date.now() / 1000 - job.started_at);
      $("run-status").textContent =
        `Astra is drawing · ${elapsed.toFixed(0)} s`;
      $("run-label").textContent = "GENERATING / GPT-6 ASTRA";
    } else if (job.id === activeRun) {
      const spaceCompleted = job.id === spaceRunId;
      activeRun = null;
      setBusy(false);
      if (job.state === "complete") {
        records = await api("/api/drawings");
        select(job.record, true);
        $("run-status").textContent =
          `Generated live in ${(job.record.metrics.latency_ms / 1000).toFixed(1)} s.`;
        if (spaceCompleted) {
          spaceRunId = null;
          try {
            await plotSpaceDrawing(job.record);
          } catch (error) {
            finishSpaceWorkflow(error.message, true);
          }
        }
      } else if (job.state === "error") {
        $("run-status").textContent = job.error;
        $("run-status").classList.add("error");
        $("run-label").textContent = "GENERATION FAILED";
        if (spaceCompleted) finishSpaceWorkflow(job.error, true);
      }
    }
    if (!status.runtime_available && !busy) {
      $("run-status").textContent =
        "Model runtime unavailable. Check server configuration.";
    }
  } catch (error) {
    $("connection").textContent = "Server disconnected";
    for (const id of ["plot-open", "calibration-open", "plot-home"])
      $(id).disabled = true;
    if (["running", "pausing"].includes(plotState.job.state))
      $("plot-status").textContent =
        "Server disconnected; plot state unknown. Use the physical pause button if needed.";
    if (busy)
      $("run-status").textContent =
        "Connection interrupted; checking the live request...";
  } finally {
    setTimeout(refresh, 1000);
  }
}
async function init() {
  try {
    records = await api("/api/drawings");
    if (records.length) select(records[0]);
  } catch (error) {
    $("run-status").textContent = error.message;
  }
  refresh();
}
init();
